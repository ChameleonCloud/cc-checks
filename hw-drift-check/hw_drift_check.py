#!/usr/bin/env python3
"""Compares this machine's hardware with its Chameleon reference-repository record.

The script needs the Python standard library (3.8 or newer). It reads /sys
and calls lsblk, lscpu, ethtool, lspci, smartctl and dmidecode when they are
installed. It changes nothing on the machine.

The comparison is a [non dynamic] mirror of doni-referenceapi
(reference_transmogrifier/models/reference_repo.py).
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

VERSION = "0.2"

VENDORDATA_CACHE = "/var/cache/vendordata2.json"
VENDORDATA_URL = "http://169.254.169.254/openstack/latest/vendor_data2.json"

REFERENCE_URL = (
    "https://raw.githubusercontent.com/ChameleonCloud/reference-repository/master/"
    "data/chameleoncloud/sites/{site}/clusters/chameleon/nodes/{node}.json"
)
REGION_TO_SITE = {
    "CHI@UC": "uc",
    "CHI@TACC": "tacc",
    "CHI@NRP": "nrp",
    "KVM@TACC": "kvm",
    "CHI@NCAR": "ncar",
}
SITES = ["uc", "tacc", "ncar", "nu", "nrp", "kvm"]

TOOLS = [
    ("lsblk", "util-linux", "disk serial number, WWN and transport"),
    ("ethtool", "ethtool", "permanent MAC address and supported link speeds"),
    ("lspci", "pciutils", "vendor and model names of NICs and NVMe drives"),
    ("smartctl", "smartmontools", "full disk firmware revision (needs root)"),
    ("lscpu", "util-linux", "CPU cache sizes, rounded the way the record rounds them"),
    ("dmidecode", "dmidecode", "CPU model, clock speed, socket and thread counts, installed memory (needs root)"),
]

# All copies of doni’s lookup tables.
NON_GPU_DISPLAY_PCI_IDS = {("102b", "0534"), ("102b", "0536"), ("1a03", "2000"), ("1002", "13c0")}
GPU_MODEL_NAMES = {("10de", "2342"): "GH200 96GB HBM3"}
FPGA_IDS = {("10ee", "903f", "028000")}
COMPOSABLE_NODE_TYPES = {"compute_liqid", "compute_gigaio"}

OK, NOTE, DIFF = "OK", "NOTE", "DIFF"
RANK = {OK: 0, NOTE: 1, DIFF: 2}
ROOT = ""

# Small helpers
def read(path):
    try:
        with open(ROOT + path, errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None

def run(command):
    try:
        done = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=dict(os.environ, LC_ALL="C"),
            universal_newlines=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout

def to_int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None

def squash(text):
    return " ".join(str(text or "").lower().split())

def find_tools():
    # sbin folders are not always on a non-root user's PATH.
    path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/local/sbin", "/usr/sbin", "/sbin"])
    return {name: shutil.which(name, path=path) for name, _package, _use in TOOLS}

def os_name():
    for line in (read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


# Gathering functions
def lspci_names(tools):
    """PCI address -> (vendor name, device name)"""
    names = {}
    if not tools.get("lspci"):
        return names
    for block in run([tools["lspci"], "-vmm", "-D"]).split("\n\n"):
        fields = dict(line.split(":\t", 1) for line in block.splitlines() if ":\t" in line)
        if "Slot" in fields:
            names[fields["Slot"]] = (fields.get("Vendor"), fields.get("Device"))
    return names


def max_supported_mbps(ethtool_output):
    """Fastest speed under "Supported link modes" in `ethtool <dev>` output, in Mb/s."""
    speeds = []
    in_block = False
    for line in ethtool_output.splitlines():
        if "Supported link modes:" in line:
            in_block = True
            line = line.split(":", 1)[1]
        elif in_block and ":" in line:
            break  # the next setting starts here
        if in_block:
            speeds += [int(speed) for speed in re.findall(r"\b(\d+)base", line)]
    return max(speeds) if speeds else None


def gather_network(tools, pci_names):
    """One dict per hardware network port, read from /sys/class/net."""
    try:
        interfaces = sorted(os.listdir(ROOT + "/sys/class/net"))
    except OSError:
        return []

    ports = []
    for name in interfaces:
        base = "/sys/class/net/" + name
        if not os.path.exists(ROOT + base + "/device"):
            continue  # software interface, eg: lo, bridge, bond, vlan, veth
        device = os.path.realpath(ROOT + base + "/device")
        bus = os.path.basename(os.path.realpath(device + "/subsystem"))

        # A bond or a user can change a port's MAC. 
        address = (read(base + "/address") or "").lower()
        permanent = ""
        if tools.get("ethtool"):
            found = re.search(
                r"Permanent address:\s*([0-9A-Fa-f:]+)", run([tools["ethtool"], "-P", name])
            )
            if found and found.group(1).strip("0:"):
                permanent = found.group(1).lower()
        permanent = permanent or (read(base + "/bonding_slave/perm_hwaddr") or "").lower()

        # Reading carrier fails while the interface is administratively down,
        # so link is None (unknown) unless the interface is up.
        try:
            admin_up = bool(int(read(base + "/flags") or "0", 16) & 1)  # IFF_UP
        except ValueError:
            admin_up = False
        carrier = read(base + "/carrier")
        link = (carrier == "1") if (admin_up and carrier in ("0", "1")) else None
        speed = to_int(read(base + "/speed")) if link else None
        if speed is not None and speed <= 0:
            speed = None

        link_type = read(base + "/type")
        slot = os.path.basename(device) if bus == "pci" else None
        vendor, model = pci_names.get(slot, (None, None))
        driver = None
        if os.path.exists(device + "/driver"):
            driver = os.path.basename(os.path.realpath(device + "/driver"))

        ports.append(
            {
                "name": name,
                "mac": permanent or address,
                "mac_is_permanent": bool(permanent) or read(base + "/addr_assign_type") == "0",
                "interface": {"1": "Ethernet", "32": "InfiniBand"}.get(link_type, "type " + str(link_type)),
                "driver": driver,
                "bus": bus,
                "pci_slot": slot,
                "vendor": vendor,
                "model": model,
                "admin_up": admin_up,
                "link": link,
                "speed_mbps": speed,
                "max_supported_mbps": (
                    max_supported_mbps(run([tools["ethtool"], name])) if tools.get("ethtool") else None
                ),
                "virtual_function": os.path.exists(device + "/physfn"),
            }
        )
    return ports


# doni-referenceapi keeps only disks with these names (inspector/utils.py).
DISK_NAME = re.compile(r"^(nvme\d+n\d+|sd[a-z]+)$")

def norm_id(value):
    """A serial number or WWN reduced to a uniform id.
    """
    text = str(value or "").strip().lower()
    for prefix in ("wwn-", "0x", "naa.", "eui.", "nvme.", "uuid."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def udev_properties(device_number):
    """udev's stored properties for a block device, such as ID_WWN."""
    properties = {}
    for line in (read("/run/udev/data/b" + device_number) or "").splitlines():
        if line.startswith("E:") and "=" in line:
            key, value = line[2:].split("=", 1)
            properties[key] = value
    return properties


def disk_paths():
    """Kernel disk name -> its name under /dev/disk/by-path."""
    paths = {}
    folder = ROOT + "/dev/disk/by-path"
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return paths
    for entry in entries:
        target = os.path.basename(os.path.realpath(os.path.join(folder, entry)))
        paths.setdefault(target, "/dev/disk/by-path/" + entry)
    return paths


def gather_disks(tools, pci_names, is_root):
    """One dict per physical disk, read from /sys/block, udev and lsblk."""
    lsblk_rows = {}
    if tools.get("lsblk"):
        try:
            listing = json.loads(run([tools["lsblk"], "-J", "-d", "-o", "NAME,SERIAL,WWN,TRAN"]) or "{}")
        except ValueError:
            listing = {}
        lsblk_rows = {row.get("name"): row for row in listing.get("blockdevices", [])}
    by_path = disk_paths()

    try:
        names = sorted(os.listdir(ROOT + "/sys/block"))
    except OSError:
        return []

    disks = []
    for name in names:
        base = "/sys/block/" + name
        sectors = to_int(read(base + "/size")) or 0
        if not DISK_NAME.match(name) or not sectors:
            continue  # not a disk, or an empty slot such as a virtual media drive
        row = lsblk_rows.get(name, {})
        udev = udev_properties(read(base + "/dev") or "")

        # Every identifier the OS offers; a record disk matches if it shares one.
        candidates = [
            row.get("serial"),
            row.get("wwn"),
            udev.get("ID_SERIAL_SHORT"),
            udev.get("ID_SCSI_SERIAL"),
            udev.get("ID_WWN"),
            udev.get("ID_WWN_WITH_EXTENSION"),
            read(base + "/wwid"),
            read(base + "/device/wwid"),
            read(base + "/device/serial"),
        ]
        ids = sorted({norm_id(value) for value in candidates} - {""})

        vendor_raw = read(base + "/device/vendor")
        if name.startswith("nvme"):
            # An NVMe drive's maker is the PCI vendor of its controller.
            controller = os.path.basename(os.path.realpath(ROOT + base + "/device/device"))
            vendor = pci_names.get(controller, (None, None))[0]
        else:
            # SATA disks report the protocol ("ATA") where a maker should be.
            vendor = None if vendor_raw in ("ATA", "NVMe") else vendor_raw

        firmware = None
        if tools.get("smartctl") and is_root:
            found = re.search(
                r"^Firmware Version:\s*(\S.*)$", run([tools["smartctl"], "-i", "/dev/" + name]), re.M
            )
            firmware = found.group(1).strip() if found else None

        disks.append(
            {
                "name": name,
                "size_bytes": sectors * 512,
                "model": read(base + "/device/model"),
                # SCSI and SATA disks expose "rev" (4 characters), NVMe "firmware_rev".
                "rev": read(base + "/device/rev") or read(base + "/device/firmware_rev"),
                "firmware": firmware,
                "serial": (
                    row.get("serial")
                    or read(base + "/device/serial")
                    or udev.get("ID_SERIAL_SHORT")
                ),
                "wwn": udev.get("ID_WWN") or row.get("wwn") or read(base + "/wwid") or read(base + "/device/wwid"),
                "ids": ids,
                "rotational": read(base + "/queue/rotational") == "1",
                "removable": read(base + "/removable") == "1",
                "transport": row.get("tran"),
                "by_path": by_path.get(name) or (
                    "/dev/disk/by-path/" + udev["ID_PATH"] if udev.get("ID_PATH") else None
                ),
                "vendor": vendor,
                "vendor_raw": vendor_raw,
            }
        )
    return disks


def parse_cpu_list(text):
    """Number of CPUs in a kernel CPU list such as "0-63,128-191"."""
    count = 0
    for part in (text or "").split(","):
        if "-" in part:
            low, high = part.split("-", 1)
            count += int(high) - int(low) + 1
        elif part.strip():
            count += 1
    return count


def human_bytes(text):
    """Bytes in a size such as "3.8 MiB", read the way pydantic's ByteSize reads it."""
    units = {"b": 1, "k": 10**3, "kb": 10**3, "kib": 2**10, "m": 10**6, "mb": 10**6, "mib": 2**20,
             "g": 10**9, "gb": 10**9, "gib": 2**30, "t": 10**12, "tb": 10**12, "tib": 2**40}
    found = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$", text or "")
    if not found or found.group(2).lower() not in units:
        return None
    return int(float(found.group(1)) * units[found.group(2).lower()])


def lscpu_caches(output):
    """Cache size per instance from `lscpu`, rounded"""
    caches = {}
    names = {"l1d": "cache_l1d", "l1i": "cache_l1i", "l2": "cache_l2", "l3": "cache_l3"}
    for line in output.splitlines():
        found = re.match(r"^\s*(L1d|L1i|L2|L3)(?: cache)?:\s*(.+?)\s*\((\d+) instances?\)", line)
        if found:
            total = human_bytes(found.group(2))
            if total is not None:
                caches[names[found.group(1).lower()]] = total // int(found.group(3))
    return caches


def sys_caches():
    """Exact cache size per instance from /sys, as a fallback when lscpu is missing or old."""
    caches = {}
    base = "/sys/devices/system/cpu/cpu0/cache"
    try:
        entries = sorted(os.listdir(ROOT + base))
    except OSError:
        return caches
    for entry in entries:
        level, kind = read("%s/%s/level" % (base, entry)), read("%s/%s/type" % (base, entry))
        size = human_bytes((read("%s/%s/size" % (base, entry)) or "").replace("K", " KiB").replace("M", " MiB"))
        if level == "1" and kind in ("Data", "Instruction"):
            caches["cache_l1" + kind[0].lower()] = size
        elif level in ("2", "3"):
            caches["cache_l" + level] = size
    return caches


def dmidecode_blocks(tools, kind):
    """Blocks of `dmidecode -t <kind>` as dicts; empty when dmidecode is missing."""
    if not tools.get("dmidecode"):  # callers pass is_root; dmidecode needs root
        return []
    blocks = []
    for chunk in run([tools["dmidecode"], "-t", kind]).split("\n\n"):
        fields = {}
        for line in chunk.splitlines():
            if line.startswith("\t") and not line.startswith("\t\t") and ":" in line:
                key, value = line.strip().split(":", 1)
                fields[key.strip()] = value.strip()
        if fields and chunk.lstrip().startswith("Handle"):
            blocks.append(fields)
    return blocks


def dmi_speed_hz(text):
    found = re.match(r"^([\d.]+)\s*(MHz|GHz)$", text or "")
    if not found:
        return None
    return int(float(found.group(1)) * (10**6 if found.group(2) == "MHz" else 10**9))


def dmi_size_bytes(text):
    """A dmidecode memory size. dmidecode's MB and GB are binary units."""
    found = re.match(r"^(\d+)\s*(kB|MB|GB|TB)$", text or "")
    if not found:
        return None
    return int(found.group(1)) * {"kB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40}[found.group(2)]


def cpuinfo_fields():
    """The first value of each key in /proc/cpuinfo."""
    fields = {}
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields.setdefault(key.strip(), value.strip())
    return fields


# ARM "CPU implementer" codes in /proc/cpuinfo.
ARM_IMPLEMENTERS = {"0x41": "ARM", "0x42": "Broadcom", "0x43": "Cavium", "0x46": "Fujitsu",
                    "0x4e": "NVIDIA", "0x50": "Ampere", "0x51": "Qualcomm", "0xc0": "Ampere"}


def gather_cpu(tools, is_root):
    """Processor and architecture facts."""
    info = cpuinfo_fields()
    packages = set()
    try:
        for entry in os.listdir(ROOT + "/sys/devices/system/cpu"):
            package = read("/sys/devices/system/cpu/%s/topology/physical_package_id" % entry)
            if re.match(r"^cpu\d+$", entry) and package is not None:
                packages.add(package)
    except OSError:
        pass
    sockets = []
    for block in dmidecode_blocks(tools, "processor") if is_root else []:
        sockets.append({
            "manufacturer": block.get("Manufacturer"),
            "version": block.get("Version"),
            "current_speed_hz": dmi_speed_hz(block.get("Current Speed")),
            "core_count": to_int(block.get("Core Count")),
            "thread_count": to_int(block.get("Thread Count")),
            "populated": "Populated" in (block.get("Status") or "") and "Unpopulated" not in (block.get("Status") or ""),
        })
    base_khz = to_int(read("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency"))
    return {
        "arch": platform.machine(),
        "model_name": info.get("model name"),
        "vendor_id": info.get("vendor_id") or ARM_IMPLEMENTERS.get((info.get("CPU implementer") or "").lower()),
        "packages": len(packages) or None,
        "present": parse_cpu_list(read("/sys/devices/system/cpu/present")) or None,
        "online": parse_cpu_list(read("/sys/devices/system/cpu/online")) or None,
        "smt_control": read("/sys/devices/system/cpu/smt/control"),
        "cmdline": read("/proc/cmdline"),
        "base_frequency_hz": base_khz * 1000 if base_khz else None,
        "caches_lscpu": lscpu_caches(run([tools["lscpu"]])) if tools.get("lscpu") else {},
        "caches_sys": sys_caches(),
        "dmi_sockets": sockets,
    }


def gather_memory(tools, is_root):
    """Installed memory (DMI, root only) and what the kernel has online."""
    dimms = []
    for block in dmidecode_blocks(tools, "17") if is_root else []:
        size = dmi_size_bytes(block.get("Size"))
        if size:
            dimms.append({"size": size, "type": block.get("Type"), "locator": block.get("Locator")})
    try:
        block_size = int(read("/sys/devices/system/memory/block_size_bytes") or "0", 16)
    except ValueError:
        block_size = 0
    online = 0
    try:
        for entry in os.listdir(ROOT + "/sys/devices/system/memory"):
            if entry.startswith("memory") and read("/sys/devices/system/memory/%s/online" % entry) == "1":
                online += 1
    except OSError:
        pass
    total_kib = None
    for line in (read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            total_kib = to_int(line.split()[1])
    try:
        nvdimm = bool(os.listdir(ROOT + "/sys/bus/nd/devices"))
    except OSError:
        nvdimm = False
    return {
        "dimms": dimms,
        "dmi_total": sum(d["size"] for d in dimms) or None,
        "online_bytes": (online * block_size) or None,
        "memtotal_bytes": total_kib * 1024 if total_kib else None,
        "nvdimm_bus": nvdimm,
    }


def gather_pci(pci_names):
    """Every PCI function: class, vendor and device ids, and its lspci names."""
    devices = []
    try:
        slots = sorted(os.listdir(ROOT + "/sys/bus/pci/devices"))
    except OSError:
        return devices
    for slot in slots:
        base = "/sys/bus/pci/devices/" + slot
        vendor_name, device_name = pci_names.get(slot, (None, None))
        devices.append({
            "slot": slot,
            "class": (read(base + "/class") or "").replace("0x", ""),
            "vendor_id": (read(base + "/vendor") or "").replace("0x", ""),
            "device_id": (read(base + "/device") or "").replace("0x", ""),
            "revision": (read(base + "/revision") or "").replace("0x", ""),
            "virtual_function": os.path.exists(ROOT + base + "/physfn"),
            "vendor_name": vendor_name,
            "device_name": device_name,
        })
    return devices


def gather_infiniband():
    """InfiniBand-layer ports from /sys/class/infiniband (RoCE ports report Ethernet)."""
    ports = []
    try:
        devices = sorted(os.listdir(ROOT + "/sys/class/infiniband"))
    except OSError:
        return ports
    for device in devices:
        try:
            numbers = sorted(os.listdir(ROOT + "/sys/class/infiniband/%s/ports" % device))
        except OSError:
            continue
        for number in numbers:
            base = "/sys/class/infiniband/%s/ports/%s" % (device, number)
            ports.append({
                "device": device,
                "port": number,
                "link_layer": read(base + "/link_layer"),
                "state": read(base + "/state"),
                "rate": read(base + "/rate"),
            })
    return ports


def gather_firmware():
    """BIOS, system and boot facts. Serial numbers in /sys are readable only by root."""
    dmi = "/sys/class/dmi/id/"
    return {
        "bios_vendor": read(dmi + "bios_vendor"),
        "bios_version": read(dmi + "bios_version"),
        "bios_date": read(dmi + "bios_date"),
        "sys_vendor": read(dmi + "sys_vendor"),
        "product_name": read(dmi + "product_name"),
        "product_serial": read(dmi + "product_serial"),
        "boot_mode": "uefi" if os.path.isdir(ROOT + "/sys/firmware/efi") else "bios",
    }


def gather():
    """Everything the check needs from this machine, as plain JSON-ready data."""
    tools = find_tools()
    is_root = os.geteuid() == 0
    pci_names = lspci_names(tools)
    return {
        "hostname": platform.node(),
        "os": os_name(),
        "kernel": platform.release(),
        "is_root": is_root,
        "tools": tools,
        "cpu": gather_cpu(tools, is_root),
        "memory": gather_memory(tools, is_root),
        "pci_devices": gather_pci(pci_names),
        "infiniband_ports": gather_infiniband(),
        "firmware": gather_firmware(),
        "network_adapters": gather_network(tools, pci_names),
        "storage_devices": gather_disks(tools, pci_names, is_root),
    }


# Comparing: record against machine
def finding(level, field, record=None, machine=None, why=""):
    return {"level": level, "field": field, "record": record, "machine": machine, "why": why}


def item(identity, label, findings):
    level = max((f["level"] for f in findings), key=RANK.get, default=OK)
    return {"id": identity, "label": label, "level": level, "findings": findings}

VENDOR_ALIASES = {
    "advanced micro devices, inc. [amd/ati]": "amd",
    "advanced micro devices, inc.": "amd",
    "american megatrends international, llc.": "ami",
    "genuineintel": "intel",
    "sk": "sk hynix",
    "hynix": "sk hynix",
    "skhynix": "sk hynix",
}

def canon_vendor(name):
    """Manufacturer reduced the way normalize_manufacturer() does: by first word."""
    text = squash(name)
    if text in VENDOR_ALIASES:
        return VENDOR_ALIASES[text]
    first = re.split(r"[^a-z0-9]+", text)[0] if text else ""
    return VENDOR_ALIASES.get(first, first)


def mac_key(mac):
    """A MAC as the record stores it: lower case, InfiniBand cut to its last 8 octets."""
    octets = [octet for octet in str(mac or "").strip().lower().split(":") if octet]
    return ":".join(octets[-8:] if len(octets) >= 8 else octets)


# lshw, which the record's "rate" comes from, cannot report more than this:
# 40 Gb/s for the 2021 snapshot distros ship, 100 Gb/s for B.02.20.
LSHW_RATE_CAPS = (40 * 10**9, 100 * 10**9)


def check_port(recorded, port):
    """Field-by-field findings for a record NIC and the machine port with its MAC."""
    out = []

    if recorded.get("device") and recorded["device"] != port["name"]:
        out.append(finding(
            NOTE, "device", recorded["device"], port["name"],
            "interface names are chosen by the OS and can differ from the inspection ramdisk",
        ))
    elif recorded.get("device"):
        out.append(finding(OK, "device", recorded["device"], port["name"]))

    if recorded.get("interface"):
        level = OK if recorded["interface"] == port["interface"] else DIFF
        out.append(finding(level, "interface", recorded["interface"], port["interface"]))

    if recorded.get("driver"):
        # lshw writes an IPoIB port's driver as "mlx5_core[ib_ipoib]".
        same = re.sub(r"\[.*\]", "", recorded["driver"]) == port["driver"]
        out.append(finding(
            OK if same else NOTE, "driver", recorded["driver"], port["driver"],
            "" if same else "the driver is software, so a different OS can load a different one",
        ))

    for field in ("vendor", "model"):
        if not recorded.get(field):
            continue
        if port[field] is None:
            out.append(finding(NOTE, field, recorded[field], None, "not checked: needs lspci"))
        elif field == "vendor":
            same = canon_vendor(recorded[field]) == canon_vendor(port[field])
            out.append(finding(OK if same else NOTE, field, recorded[field], port[field],
                               "" if same else "same MAC, so the card is the same; the name differs"))
        else:
            same = squash(recorded[field]) == squash(port[field])
            out.append(finding(
                OK if same else NOTE, field, recorded[field], port[field],
                "" if same else "same MAC, so the card is the same; names come from each tool's PCI ID list",
            ))

    rate = recorded.get("rate")
    supported = port["max_supported_mbps"] and port["max_supported_mbps"] * 10**6
    speed = port["speed_mbps"] and port["speed_mbps"] * 10**6
    if rate and supported:
        if supported == rate:
            out.append(finding(OK, "rate", rate, supported))
        elif supported > rate and rate in LSHW_RATE_CAPS:
            out.append(finding(NOTE, "rate", rate, supported,
                               "the record comes from lshw, which reports at most 40 or 100 Gb/s"))
        else:
            out.append(finding(DIFF, "rate", rate, supported,
                               "fastest supported link mode; a different transceiver or cable changes it"))
        if speed and speed < min(rate, supported):
            out.append(finding(NOTE, "link speed", rate, speed,
                               "the link is running slower than the port's rated speed"))
    elif rate and speed:
        out.append(finding(
            OK if speed == rate else NOTE, "rate", rate, speed,
            "machine value is the current link speed; install ethtool to compare the fastest supported mode",
        ))
    elif rate:
        out.append(finding(NOTE, "rate", rate, None, "not checked: needs ethtool, or the interface up"))

    # The generator only records ports that had link, so enabled is true there.
    if recorded.get("enabled") is True:
        if port["link"] is True:
            out.append(finding(OK, "enabled", True, True))
        elif port["link"] is False:
            out.append(finding(DIFF, "enabled", True, False,
                               "the port had link at inspection and has none now: cable or switch port"))
        else:
            out.append(finding(NOTE, "enabled", True, None,
                               "not checked: the interface is down in this OS, so link state is unknown"))
    return out


def compare_network(recorded_ports, ports):
    hardware = [port for port in ports if not port["virtual_function"]]
    by_mac = {}
    for port in hardware:
        by_mac.setdefault(mac_key(port["mac"]), []).append(port)
    mac_unknown = [port["name"] for port in hardware if not port["mac_is_permanent"]]

    items = []
    for recorded in recorded_ports:
        key = mac_key(recorded.get("mac"))
        label = recorded.get("device") or "?"
        if not key:
            items.append(item("(no MAC)", label, [finding(
                NOTE, "presence", "listed", None,
                "the record has no MAC for this port, so it cannot be matched",
            )]))
        elif by_mac.get(key):
            port = by_mac[key].pop(0)
            items.append(item(key, port["name"], check_port(recorded, port)))
        else:
            why = "no port on the machine has this MAC"
            if mac_unknown:
                why += "; " + ", ".join(mac_unknown) + " may have a changed MAC (install ethtool to read the permanent one)"
            items.append(item(key, label, [finding(DIFF, "presence", "listed", "not found", why)]))

    for key in sorted(by_mac):
        for port in by_mac[key]:
            if port["bus"] != "pci":
                found = finding(NOTE, "presence", "not listed", "present",
                                "not a PCI device (%s), so not a fixed network card" % port["bus"])
            elif port["link"] is True:
                found = finding(DIFF, "presence", "not listed", "present, with link",
                                "the record should list every port that has link")
            else:
                found = finding(
                    NOTE, "presence", "not listed",
                    "present, no link" if port["link"] is False else "present, interface down",
                    "expected: the record only lists ports that had link at inspection",
                )
            items.append(item(key, port["name"], [found]))

    notes = []
    functions = len(ports) - len(hardware)
    if functions:
        notes.append("%d SR-IOV virtual function(s) ignored" % functions)
    return {"record_count": len(recorded_ports), "machine_count": len(hardware), "items": items, "notes": notes}


def gigabytes(size_bytes):
    """Whole decimal gigabytes. The generator stores a disk size rounded down to this."""
    return (to_int(size_bytes) or 0) // 10**9


def same_model(a, b):
    """True if one model string starts with the other; the kernel cuts SCSI models at 16 characters."""
    a, b = squash(a), squash(b)
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def guess_interface(disk):
    """The generator's guess at a disk's interface, from the shape of its by-path name."""
    path = disk["by_path"] or ""
    if disk["vendor_raw"] == "NVMe":
        return "PCIe"
    for marker, interface in (("scsi", "SAS"), ("sas", "SAS"), ("nvme", "PCIe"), ("ata", "SATA")):
        if marker in path:
            return interface
    return None


TRANSPORT_INTERFACE = {"sata": "SATA", "ata": "SATA", "sas": "SAS", "nvme": "PCIe"}


def share_prefix(a, b):
    """True for identifiers where one extends the other, like a WWN and its extended form."""
    return a == b or (min(len(a), len(b)) >= 8 and (a.startswith(b) or b.startswith(a)))


def check_disk(recorded, disk, matched_by):
    """Field-by-field findings for a record disk and the machine disk it matched."""
    out = []
    has_id = bool(recorded.get("serial") or recorded.get("wwn"))

    if matched_by == "id":
        if recorded.get("serial"):
            same = norm_id(recorded["serial"]) in disk["ids"]
            out.append(finding(OK if same else NOTE, "serial", recorded["serial"], disk["serial"],
                               "" if same else "matched on WWN; tools disagree on what a SAS disk's serial is"))
        if recorded.get("wwn"):
            same = any(share_prefix(norm_id(recorded["wwn"]), known) for known in disk["ids"])
            out.append(finding(OK if same else NOTE, "wwn", recorded["wwn"], disk["wwn"],
                               "" if same else "matched on serial number; the WWN is written differently"))
    elif has_id:
        out.append(finding(DIFF, "serial", recorded.get("serial") or recorded.get("wwn"), disk["serial"],
                           "same model and size but a different serial: the disk was probably replaced"))
    else:
        out.append(finding(NOTE, "serial", None, disk["serial"],
                           "the record has no serial or WWN, so this was matched on model and size"))

    recorded_name = str(recorded.get("device") or "").replace("/dev/", "")
    out.append(finding(OK, "device", recorded_name, disk["name"],
                       "" if recorded_name == disk["name"] else "disk names are assigned at boot and may change"))

    same = gigabytes(recorded.get("size")) == gigabytes(disk["size_bytes"])
    out.append(finding(OK if same else DIFF, "size", recorded.get("size"), disk["size_bytes"],
                       "" if same else "compared in whole GB"))

    if recorded.get("model"):
        same = same_model(recorded["model"], disk["model"])
        out.append(finding(OK if same else DIFF, "model", recorded["model"], disk["model"]))

    if recorded.get("media_type"):
        wanted = "Rotational" if recorded["media_type"] in ("HDD", "Rotational") else recorded["media_type"]
        actual = "Rotational" if disk["rotational"] else "SSD"
        out.append(finding(OK if wanted == actual else DIFF, "media_type", recorded["media_type"], actual))

    if recorded.get("interface"):
        # Accept the generator's own guess, or what the kernel says the transport is.
        guess = guess_interface(disk)
        actual = TRANSPORT_INTERFACE.get(disk["transport"] or "")
        if recorded["interface"] in (guess, actual):
            out.append(finding(OK, "interface", recorded["interface"], recorded["interface"]))
        else:
            out.append(finding(NOTE, "interface", recorded["interface"], actual or guess,
                               "the record guesses this from the device path, so a SATA disk on a SAS controller reads SAS"))

    if recorded.get("rev"):
        wanted = str(recorded["rev"]).strip()
        short = (disk["rev"] or "").strip()
        if wanted in (disk["firmware"], short):
            out.append(finding(OK, "rev", wanted, disk["firmware"] or short))
        elif not disk["firmware"] and short and (wanted.endswith(short) or wanted.startswith(short)):
            out.append(finding(OK, "rev", wanted, short,
                               "the kernel shows 4 characters of it; smartctl as root shows all"))
        else:
            out.append(finding(DIFF, "rev", wanted, disk["firmware"] or short or None,
                               "firmware revision"))

    if recorded.get("vendor"):
        if not disk["vendor"]:
            out.append(finding(NOTE, "vendor", recorded["vendor"], None,
                               "not checked: the OS reports no manufacturer for this disk"))
        else:
            same = canon_vendor(recorded["vendor"]) == canon_vendor(disk["vendor"])
            out.append(finding(OK if same else NOTE, "vendor", recorded["vendor"], disk["vendor"],
                               "" if same else "same serial, so the disk is the same; the name differs"))
    return out


def compare_storage(recorded_disks, disks):
    free = list(disks)
    matches = {}  # index of record disk -> (machine disk, how it matched)

    # First by serial number or WWN, which identify one physical disk.
    for exact in (True, False):
        for index, recorded in enumerate(recorded_disks):
            if index in matches:
                continue
            keys = {norm_id(recorded.get("serial")), norm_id(recorded.get("wwn"))} - {""}
            for disk in free:
                if exact:
                    hit = bool(keys & set(disk["ids"]))
                else:
                    hit = any(share_prefix(key, known) for key in keys for known in disk["ids"])
                if hit:
                    matches[index] = (disk, "id")
                    free.remove(disk)
                    break

    # Then by model and size: hand-written records have no serial, and a
    # replaced disk has a new one.
    for index, recorded in enumerate(recorded_disks):
        if index in matches:
            continue
        for disk in free:
            if same_model(recorded.get("model"), disk["model"]) and (
                gigabytes(recorded.get("size")) == gigabytes(disk["size_bytes"])
            ):
                matches[index] = (disk, "model")
                free.remove(disk)
                break

    items = []
    for index, recorded in enumerate(recorded_disks):
        identity = recorded.get("serial") or recorded.get("wwn") or "(no serial)"
        if index in matches:
            disk, matched_by = matches[index]
            items.append(item(identity, disk["name"], check_disk(recorded, disk, matched_by)))
        else:
            label = "%s %s" % (recorded.get("device") or "?", recorded.get("model") or "")
            items.append(item(identity, label.strip(), [finding(
                DIFF, "presence", "listed", "not found",
                "no disk on the machine has this serial, WWN, or model and size",
            )]))

    for disk in free:
        label = "%s %s" % (disk["name"], disk["model"] or "")
        if disk["removable"] or disk["transport"] in ("usb", "iscsi", "fc"):
            found = finding(NOTE, "presence", "not listed", "present",
                            "removable or network-attached (%s), so not the node's fixed hardware"
                            % (disk["transport"] or "removable"))
        else:
            found = finding(DIFF, "presence", "not listed", "present",
                            "%d GB disk on the machine that the record does not list"
                            % gigabytes(disk["size_bytes"]))
        items.append(item(disk["serial"] or disk["wwn"] or "(no serial)", label.strip(), [found]))

    return {"record_count": len(recorded_disks), "machine_count": len(disks), "items": items, "notes": []}


def scalar_section(title, items, notes=None):
    """A section about one part of the node, rather than a list of devices."""
    return {"title": title, "items": items, "notes": notes or []}


def compare_architecture(record, cpu):
    arch = record.get("architecture") or {}
    out = []
    if arch.get("platform_type"):
        out.append(finding(OK if arch["platform_type"] == cpu["arch"] else DIFF,
                           "platform_type", arch["platform_type"], cpu["arch"]))

    sockets = cpu.get("dmi_sockets") or []
    if arch.get("smp_size") is not None:
        if sockets:
            empty = [s for s in sockets if not s["populated"]]
            out.append(finding(OK if len(sockets) == arch["smp_size"] else DIFF, "smp_size",
                               arch["smp_size"], len(sockets),
                               "%d socket(s) are empty" % len(empty) if empty else ""))
        elif cpu.get("packages"):
            same = cpu["packages"] == arch["smp_size"]
            out.append(finding(OK if same else DIFF, "smp_size", arch["smp_size"], cpu["packages"],
                               "" if same else "counted from CPU topology, because there was no DMI data (needs root and dmidecode)"))
        else:
            out.append(finding(NOTE, "smp_size", arch["smp_size"], None, "not checked: no socket information"))

    if arch.get("smt_size") is not None:
        dmi_threads = sum(s["thread_count"] or 0 for s in sockets) if sockets else None
        if dmi_threads:
            out.append(finding(OK if dmi_threads == arch["smt_size"] else DIFF, "smt_size",
                               arch["smt_size"], dmi_threads, "" if dmi_threads == arch["smt_size"] else
                               "sum of DMI thread counts, the same source the record uses"))
        elif cpu.get("present"):
            limited = (cpu.get("smt_control") in ("off", "forceoff")
                       or re.search(r"\b(nosmt|maxcpus=|nr_cpus=)", cpu.get("cmdline") or ""))
            if cpu["present"] == arch["smt_size"]:
                out.append(finding(OK, "smt_size", arch["smt_size"], cpu["present"]))
            elif limited and cpu["present"] < arch["smt_size"]:
                out.append(finding(NOTE, "smt_size", arch["smt_size"], cpu["present"],
                                   "the OS has SMT or CPUs turned off (%s); the record counts hardware threads"
                                   % (cpu.get("smt_control") or "kernel command line")))
            else:
                out.append(finding(DIFF, "smt_size", arch["smt_size"], cpu["present"],
                                   "hardware threads the kernel found"))
        if cpu.get("online") and cpu.get("present") and cpu["online"] < cpu["present"]:
            out.append(finding(NOTE, "online CPUs", cpu["present"], cpu["online"],
                               "some CPUs are offline in this OS"))
    return out


def cpu_model_matches(recorded, machine):
    return bool(recorded and machine) and squash(recorded) == squash(machine)


def compare_processor(record, cpu):
    proc = record.get("processor") or {}
    out = []
    sockets = [s for s in cpu.get("dmi_sockets") or [] if s["populated"]] or cpu.get("dmi_sockets") or []
    # Hand-written records put the full name in other_description.
    model = proc.get("other_description") if proc.get("model") in ("Intel Xeon",) else proc.get("model")

    if proc.get("instruction_set"):
        out.append(finding(OK if proc["instruction_set"] == cpu["arch"] else DIFF,
                           "instruction_set", proc["instruction_set"], cpu["arch"]))

    if model:
        if sockets:
            versions = sorted({s["version"] for s in sockets if s["version"]})
            same = any(cpu_model_matches(model, v) for v in versions)
            out.append(finding(OK if same else DIFF, "model", model, ", ".join(versions) or None))
            if len(versions) > 1:
                out.append(finding(DIFF, "mixed models", model, ", ".join(versions),
                                   "the sockets hold different CPU models"))
        elif cpu_model_matches(model, cpu.get("model_name")):
            out.append(finding(OK, "model", model, cpu["model_name"]))
        else:
            out.append(finding(NOTE, "model", model, cpu.get("model_name"),
                               "the record uses the DMI name; ARM CPUs and some others use a different name "
                               "in /proc/cpuinfo. Run as root with dmidecode to compare exactly"))

    if proc.get("vendor"):
        machine_vendor = sockets[0]["manufacturer"] if sockets else cpu.get("vendor_id")
        if not machine_vendor:
            out.append(finding(NOTE, "vendor", proc["vendor"], None, "not checked: no vendor information"))
        else:
            same = canon_vendor(proc["vendor"]) == canon_vendor(machine_vendor)
            out.append(finding(OK if same else (DIFF if sockets else NOTE), "vendor", proc["vendor"],
                               machine_vendor, "" if same or sockets else
                               "the ARM implementer code can name the core designer, not the chip maker"))

    if proc.get("clock_speed"):
        dmi_speed = sockets[0]["current_speed_hz"] if sockets else None
        rated = None
        found = re.search(r"@\s*([\d.]+)\s*GHz", cpu.get("model_name") or "")
        if found:
            rated = int(float(found.group(1)) * 10**9)
        machine_speed = dmi_speed or cpu.get("base_frequency_hz") or rated
        if not machine_speed:
            out.append(finding(NOTE, "clock_speed", proc["clock_speed"], None,
                               "not checked: run as root with dmidecode"))
        else:
            same = machine_speed == proc["clock_speed"]
            out.append(finding(OK if same else NOTE, "clock_speed", proc["clock_speed"], machine_speed,
                               "" if same else "a BIOS setting can change the speed DMI reports"
                               if dmi_speed else "compared with the base frequency; run as root for the DMI value"))

    exact, rounded = cpu.get("caches_sys") or {}, cpu.get("caches_lscpu") or {}
    for key in ("cache_l1d", "cache_l1i", "cache_l2", "cache_l3"):
        if proc.get(key) is None:
            continue
        wanted = proc[key]
        if rounded.get(key) == wanted or exact.get(key) == wanted:
            out.append(finding(OK, key, wanted, rounded.get(key) or exact.get(key)))
        elif exact.get(key) and abs(exact[key] - wanted) <= wanted * 0.05:
            out.append(finding(OK, key, wanted, exact[key],
                               "the record divides lscpu's rounded total, so it is off by a few percent"))
        elif rounded.get(key) or exact.get(key):
            out.append(finding(DIFF, key, wanted, rounded.get(key) or exact.get(key), "cache per instance"))
        else:
            out.append(finding(NOTE, key, wanted, None, "not checked: the OS reports no cache size"))
    return out


def compare_memory(record, memory, node_type):
    wanted = (record.get("main_memory") or {}).get("ram_size")
    if not wanted:
        return []
    gib = 2**30
    # Some hand-written records hold decimal gigabytes but are labelled GiB.
    decimal = wanted % 10**9 == 0 and wanted % gib != 0
    targets = {wanted, (wanted // 10**9) * gib} if decimal else {wanted}
    installed, online, memtotal = memory.get("dmi_total"), memory.get("online_bytes"), memory.get("memtotal_bytes")
    special = memory.get("nvdimm_bus") or node_type == "compute_nvdimm"

    if installed:
        if installed in targets:
            return [finding(OK, "ram_size", wanted, installed,
                            "the record was written in decimal GB" if installed != wanted else "")]
        if any(abs(value - wanted) <= wanted * 0.01 for value in (online, memtotal) if value):
            return [finding(NOTE, "ram_size", wanted, installed,
                            "the record holds memory as an OS saw it, not the installed DIMMs (old record)")]
        why = "sum of installed DIMMs from DMI, the same data lshw reads"
        if special:
            why += "; this node has persistent memory, which DMI lists as DIMMs"
        return [finding(DIFF, "ram_size", wanted, installed, why)]

    seen = online or memtotal
    if not seen:
        return [finding(NOTE, "ram_size", wanted, None, "not checked: no memory information")]
    nearest = min(targets, key=lambda t: abs(t - seen))
    if abs(seen - nearest) <= nearest * 0.03:
        return [finding(OK, "ram_size", wanted, seen,
                        "compared within 3%% using %s; run as root with dmidecode for the exact value"
                        % ("online memory blocks" if online else "MemTotal"))]
    if special:
        return [finding(NOTE, "ram_size", wanted, seen,
                        "persistent memory changes what the OS sees; run as root with dmidecode")]
    if seen > nearest and record.get("architecture", {}).get("platform_type") == "aarch64":
        return [finding(NOTE, "ram_size", wanted, seen,
                        "on Grace Hopper the GPU's memory can show up as system memory")]
    return [finding(DIFF, "ram_size", wanted, seen,
                    "%s, more than 3%% from the record; run as root with dmidecode to confirm"
                    % ("online memory blocks" if online else "MemTotal"))]


def gpu_devices(pci_devices):
    return [d for d in pci_devices
            if d["class"].startswith("03") and not d["virtual_function"]
            and (d["vendor_id"], d["device_id"]) not in NON_GPU_DISPLAY_PCI_IDS]


def model_core(name):
    """The part of a pci.ids name in brackets, which is the marketing name."""
    found = re.search(r"\[([^\]]+)\]\s*$", name or "")
    return squash(found.group(1) if found else name)


def compare_gpu(record, pci_devices, have_names):
    gpu = record.get("gpu")
    if not gpu:
        return [finding(NOTE, "gpu", None, None, "the record has no gpu field (hand-written record)")]
    found = gpu_devices(pci_devices)
    node_type = record.get("node_type")
    out = []
    machine_has = bool(found)
    if gpu.get("gpu") != machine_has:
        level = NOTE if node_type in COMPOSABLE_NODE_TYPES else DIFF
        out.append(finding(level, "gpu", gpu.get("gpu"), machine_has,
                           "GPUs on a PCIe fabric can be attached and detached" if level == NOTE else ""))
    else:
        out.append(finding(OK, "gpu", gpu.get("gpu"), machine_has))
    if not gpu.get("gpu") and not machine_has:
        return out

    if gpu.get("gpu_count") is not None:
        same = gpu["gpu_count"] == len(found)
        level = OK if same else (NOTE if node_type in COMPOSABLE_NODE_TYPES else DIFF)
        out.append(finding(level, "gpu_count", gpu["gpu_count"], len(found),
                           "" if same else "GPUs on a PCIe fabric can be attached and detached"
                           if level == NOTE else "display-class PCI devices, counted as the record counts them"))

    dead = [d["slot"] for d in found if d["revision"] == "ff"]
    if dead:
        out.append(finding(DIFF, "responding", "yes", "no: " + ", ".join(dead),
                           "PCI revision ff: the GPU is not answering (often called fallen off the bus)"))

    if gpu.get("gpu_model") and found:
        names = [GPU_MODEL_NAMES.get((d["vendor_id"], d["device_id"]), d["device_name"]) for d in found]
        if not have_names:
            out.append(finding(NOTE, "gpu_model", gpu["gpu_model"], None, "not checked: needs lspci"))
        elif squash(gpu["gpu_model"]) in ("nvidia corporation", ""):
            out.append(finding(NOTE, "gpu_model", gpu["gpu_model"], names[0],
                               "the record has no model: pci.ids had no name for it at inspection"))
        elif any(model_core(gpu["gpu_model"]) == model_core(n) for n in names if n):
            out.append(finding(OK, "gpu_model", gpu["gpu_model"], names[0]))
        else:
            out.append(finding(DIFF, "gpu_model", gpu["gpu_model"], ", ".join(sorted(set(n or "?" for n in names))),
                               "compared on the bracketed name from pci.ids"))
        if len(set(names)) > 1:
            out.append(finding(NOTE, "mixed models", gpu["gpu_model"], ", ".join(sorted(set(n or "?" for n in names))),
                               "the record keeps only the first GPU's model"))
    if gpu.get("gpu_vendor") and found:
        vendor = found[0]["vendor_name"]
        if vendor:
            same = canon_vendor(gpu["gpu_vendor"]) == canon_vendor(vendor)
            out.append(finding(OK if same else DIFF, "gpu_vendor", gpu["gpu_vendor"], vendor))
    return out


def compare_fpga(record, pci_devices):
    wanted = record.get("fpga")
    found = [d for d in pci_devices if not d["virtual_function"] and (
        d["class"].startswith("12") or (d["vendor_id"], d["device_id"], d["class"]) in FPGA_IDS)]
    if not wanted and not found:
        return [finding(OK, "fpga", None, None)]
    if not wanted:
        names = ", ".join(sorted({"%s %s" % (d["vendor_name"] or d["vendor_id"], d["device_name"] or d["device_id"])
                                  for d in found}))
        return [finding(DIFF, "fpga", None, names,
                        "a processing-accelerator PCI device (class 12) the record does not list")]
    if not found:
        return [finding(DIFF, "fpga", wanted.get("board_model"), None, "no FPGA on the PCI bus")]
    device = found[0]
    out = []
    if device["vendor_name"]:
        same = canon_vendor(wanted.get("board_vendor")) == canon_vendor(device["vendor_name"])
        out.append(finding(OK if same else DIFF, "board_vendor", wanted.get("board_vendor"), device["vendor_name"]))
    if device["device_name"]:
        same = squash(wanted.get("board_model")) == squash(device["device_name"])
        out.append(finding(OK if same else NOTE, "board_model", wanted.get("board_model"), device["device_name"],
                           "" if same else "an FPGA's PCI ID changes with the design loaded on it"))
    return out or [finding(NOTE, "fpga", wanted.get("board_model"), device["slot"], "names need lspci")]


def compare_infiniband(record, ib_ports, network):
    if "infiniband" not in record:
        return [finding(NOTE, "infiniband", None, None, "the record has no infiniband field")]
    ib = [p for p in ib_ports if p["link_layer"] == "InfiniBand"]
    active = [p for p in ib if "ACTIVE" in (p["state"] or "")]
    ipoib = [p for p in network if p["interface"] == "InfiniBand"]
    present = bool(ib or ipoib)
    if record["infiniband"]:
        if present:
            return [finding(OK, "infiniband", True, True)]
        return [finding(DIFF, "infiniband", True, False,
                        "no InfiniBand port in /sys/class/infiniband or the network interfaces")]
    if active:
        return [finding(DIFF, "infiniband", False, True,
                        "an InfiniBand port is active (%s); the record says the node has none"
                        % ", ".join("%s/%s" % (p["device"], p["port"]) for p in active))]
    if present:
        return [finding(NOTE, "infiniband", False, True,
                        "an InfiniBand port exists without link; the record only counts ports with link")]
    return [finding(OK, "infiniband", False, False)]


def iso_date(text):
    found = re.match(r"^0*(\d{1,2})/(\d{1,2})/(\d{4})$", text or "")
    if found:
        return "%s-%02d-%02d" % (found.group(3), int(found.group(1)), int(found.group(2)))
    return text


def compare_firmware(record, firmware):
    bios, chassis = record.get("bios") or {}, record.get("chassis") or {}
    bios_items, chassis_items, boot_items = [], [], []

    if bios.get("vendor") and firmware.get("bios_vendor"):
        same = canon_vendor(bios["vendor"]) == canon_vendor(firmware["bios_vendor"])
        bios_items.append(finding(OK if same else DIFF, "vendor", bios["vendor"], firmware["bios_vendor"]))
    if not any(firmware.get(k) for k in ("bios_vendor", "bios_version", "sys_vendor", "product_name")):
        unknown = finding(NOTE, "dmi", None, None, "not checked: the OS shows no DMI data in /sys/class/dmi/id")
        bios_items.append(unknown)
        chassis_items.append(unknown)
        bios = dict(bios, version=None)
        chassis = {}
    if bios.get("version"):
        same = squash(bios["version"]) == squash(firmware.get("bios_version"))
        bios_items.append(finding(OK if same else DIFF, "version", bios["version"], firmware.get("bios_version"),
                                  "" if same else "firmware changed since the record was written"))
    if bios.get("release_date") and firmware.get("bios_date"):
        machine = iso_date(firmware["bios_date"])
        wanted = iso_date(bios["release_date"])  # hand-written records use MM/DD/YYYY
        bios_items.append(finding(OK if machine == wanted else DIFF, "release_date", bios["release_date"], machine))

    if chassis.get("manufacturer") and firmware.get("sys_vendor"):
        same = canon_vendor(chassis["manufacturer"]) == canon_vendor(firmware["sys_vendor"])
        chassis_items.append(finding(OK if same else NOTE, "manufacturer", chassis["manufacturer"],
                                     firmware["sys_vendor"], "" if same else
                                     "the record maps some makers to another name"))
    if chassis.get("name") and firmware.get("product_name"):
        wanted = squash(chassis["name"].split("(")[0])
        machine = squash(firmware["product_name"].split("(")[0])
        same = wanted == machine or wanted.endswith(" " + machine) or machine.endswith(" " + wanted)
        chassis_items.append(finding(OK if same else DIFF, "name", chassis["name"], firmware["product_name"]))
    if chassis.get("serial") and chassis["serial"] not in ("-", "Not Specified"):
        serial = firmware.get("product_serial")
        if serial:
            chassis_items.append(finding(OK if squash(serial) == squash(chassis["serial"]) else DIFF,
                                         "serial", chassis["serial"], serial,
                                         "" if squash(serial) == squash(chassis["serial"]) else
                                         "a new system serial usually means a replaced motherboard"))
        else:
            chassis_items.append(finding(NOTE, "serial", chassis["serial"], None,
                                         "not checked: /sys/class/dmi/id/product_serial is readable only by root"))

    if record.get("boot_mode"):
        boot_items.append(finding(OK if record["boot_mode"] == firmware.get("boot_mode") else DIFF,
                                  "boot_mode", record["boot_mode"], firmware.get("boot_mode")))
    return bios_items, chassis_items, boot_items


def compare(record, machine):
    cpu = machine.get("cpu")
    pci = machine.get("pci_devices")
    sections = {}
    if cpu is not None:  # files saved by version 0.1 have no CPU data
        sections["architecture"] = scalar_section(
            "architecture", [item("architecture", "", compare_architecture(record, cpu))])
        sections["processor"] = scalar_section(
            "processor", [item("processor", record.get("processor", {}).get("model") or "",
                               compare_processor(record, cpu))])
    if machine.get("memory") is not None:
        sections["main_memory"] = scalar_section(
            "main_memory", [item("main_memory", "", compare_memory(record, machine["memory"],
                                                                     record.get("node_type")))])
    if pci is not None:
        have_names = any(d["device_name"] for d in pci)
        sections["gpu"] = scalar_section("gpu", [item("gpu", "", compare_gpu(record, pci, have_names))])
        sections["fpga"] = scalar_section("fpga", [item("fpga", "", compare_fpga(record, pci))])
        sections["infiniband"] = scalar_section("infiniband", [item(
            "infiniband", "", compare_infiniband(record, machine.get("infiniband_ports") or [],
                                                 machine["network_adapters"]))])
    if machine.get("firmware") is not None:
        bios, chassis, boot = compare_firmware(record, machine["firmware"])
        sections["bios"] = scalar_section("bios", [item("bios", "", bios)])
        sections["chassis"] = scalar_section("chassis", [item("chassis", "", chassis)])
        sections["boot_mode"] = scalar_section("boot_mode", [item("boot_mode", "", boot)])
    sections["network_adapters"] = compare_network(
        record.get("network_adapters") or [], machine["network_adapters"])
    sections["storage_devices"] = compare_storage(
        record.get("storage_devices") or [], machine["storage_devices"])
    for name in [n for n, sec in sections.items() if "title" in sec and not any(e["findings"] for e in sec["items"])]:
        del sections[name]  # nothing in the record to compare
    counts = {OK: 0, NOTE: 0, DIFF: 0}
    for section in sections.values():
        for entry in section["items"]:
            for found in entry["findings"]:
                counts[found["level"]] += 1
    return {"sections": sections, "counts": counts}


# Finding the record
class CheckError(Exception):

    def read_vendordata():
        data = {}
        try:
            with open(VENDORDATA_CACHE) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            pass
        if not (isinstance(data, dict) and (data.get("chameleon") or {}).get("node")):
            try:
                # The metadata address is link-local, so never send it to a proxy.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(VENDORDATA_URL, timeout=5) as response:
                    data = json.loads(response.read().decode())
            except (OSError, ValueError):
                data = {}
        return (data.get("chameleon") or {}) if isinstance(data, dict) else {}


    def fetch_json(url):
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode())


    def load_reference(args):
        """Returns (record, where it came from)."""
        if args.reference:
            try:
                if re.match(r"^https?://", args.reference):
                    return fetch_json(args.reference), args.reference
                with open(args.reference) as handle:
                    return json.load(handle), args.reference
            except (OSError, ValueError) as error:
                raise CheckError("cannot read the reference record %s: %s" % (args.reference, error))

        vendordata = {} if (args.node and args.site) else read_vendordata()
        node = args.node or vendordata.get("node")
        if not node:
            raise CheckError(
                "no node id in vendordata. This is expected on a virtual machine (KVM) "
                "or off Chameleon. Pass --node UUID, or --reference FILE."
            )
        first = args.site or REGION_TO_SITE.get(vendordata.get("region"))
        sites = [args.site] if args.site else ([first] if first else []) + [s for s in SITES if s != first]
        for site in sites:
            url = REFERENCE_URL.format(site=site, node=node)
            try:
                return fetch_json(url), url
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise CheckError("cannot download %s: %s" % (url, error))
            except (OSError, ValueError) as error:
                raise CheckError("cannot download %s: %s" % (url, error))
        raise CheckError("node %s has no record in the reference-repository (looked in: %s)"
                        % (node, ", ".join(sites)))


# Reporting
def show(field, value):
    if value is None or value == "":
        return "none"
    if field in ("rate", "link speed") and isinstance(value, int):
        return "%g Gb/s" % (value / 10**9)
    if field == "size" and isinstance(value, int):
        return "%d GB" % gigabytes(value)
    if field == "ram_size" and isinstance(value, int):
        return "%s bytes (%.1f GiB)" % (value, value / 2**30)
    if field == "clock_speed" and isinstance(value, int):
        return "%g GHz" % (value / 10**9)
    if field.startswith("cache_") and isinstance(value, int):
        return "%d bytes (%.1f KiB)" % (value, value / 1024)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render(record, source, machine, result, verbose):
    lines = ["Hardware drift check %s" % VERSION, ""]
    lines.append("Node:      %s (%s, %s)" % (
        record.get("uid", "?"), record.get("node_name", "?"), record.get("node_type", "?")))
    lines.append("Record:    %s" % source)
    lines.append("Machine:   %s, %s, kernel %s, %s" % (
        machine.get("hostname"), machine.get("os"), machine.get("kernel"),
        "run as root" if machine.get("is_root") else "not run as root"))

    for name, section in result["sections"].items():
        if "title" in section:
            entry = section["items"][0]
            lines += ["", "%-5s %s" % (entry["level"], name)]
        else:
            lines += ["", "%s: %d in the record, %d on the machine" % (
                name, section["record_count"], section["machine_count"])]
        for entry in section["items"]:
            if "title" not in section:
                lines.append("  %-5s %-24s %s" % (entry["level"], entry["id"], entry["label"]))
            for found in entry["findings"]:
                if found["level"] == OK and not verbose:
                    continue
                lines.append("          %-4s %s: record %s, machine %s" % (
                    found["level"], found["field"],
                    show(found["field"], found["record"]), show(found["field"], found["machine"])))
                if found["why"]:
                    lines.append("               %s" % found["why"])
        for note in section["notes"]:
            lines.append("  (%s)" % note)

    lines += ["", "Tools"]
    for name, package, use in TOOLS:
        path = (machine.get("tools") or {}).get(name)
        if path:
            lines.append("  %-9s %s" % (name, path))
        else:
            lines.append("  %-9s not installed. Used for: %s. Add it with: apt install %s" % (name, use, package))
    if not machine.get("is_root"):
        skipped = [t for t in ("smartctl", "dmidecode") if (machine.get("tools") or {}).get(t)]
        if skipped:
            lines.append("  %s skipped because the check was not run as root." % " and ".join(skipped))

    counts = result["counts"]
    lines += ["", "Result: %d DIFF, %d NOTE, %d OK" % (counts[DIFF], counts[NOTE], counts[OK])]
    if not verbose and counts[OK]:
        lines.append("Fields that match are hidden; pass -v to list them.")
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Compare this machine's hardware with its Chameleon reference-repository record.",
    )
    parser.add_argument("--node", help="node UUID (default: read from vendordata)")
    parser.add_argument("--site", help="site folder such as uc or tacc (default: from vendordata)")
    parser.add_argument("--reference", metavar="FILE_OR_URL",
                        help="use this record instead of downloading one")
    parser.add_argument("--save-machine", metavar="FILE",
                        help="write what was gathered from this machine to FILE as JSON")
    parser.add_argument("--machine", metavar="FILE",
                        help="compare a file written by --save-machine instead of this machine")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="also list fields that match")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.machine:
            with open(args.machine) as handle:
                machine = json.load(handle)
        else:
            machine = gather()
        if args.save_machine:
            with open(args.save_machine, "w") as handle:
                json.dump(machine, handle, indent=2, sort_keys=True)
        record, source = load_reference(args)
    except CheckError as error:
        print("hw_drift_check: %s" % error, file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print("hw_drift_check: %s" % error, file=sys.stderr)
        return 2

    result = compare(record, machine)
    if args.json:
        print(json.dumps({"node": record.get("uid"), "record": source, "machine": machine, **result},
                         indent=2, sort_keys=True))
    else:
        print(render(record, source, machine, result, args.verbose))
    return 1 if result["counts"][DIFF] else 0


if __name__ == "__main__":
    sys.exit(main())