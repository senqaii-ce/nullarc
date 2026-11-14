#!/usr/bin/env python3
"""phxbootnext: wake from hibernation straight into OpenCore when another boot loader comes first.

A wake from hibernation (the only sleep on Phoenix laptops, docs/SLEEP.md) is a cold boot: the firmware starts its
first boot option. When that is a menu (Limine, GRUB, rEFInd) in front of OpenCore, the menu shows up on every wake and
has to pick the right OpenCore by hand. UEFI has a one-shot variable for exactly this: BootNext names the Boot#### to
start on the next boot only, and the firmware deletes it once used.

    setup   adds a firmware boot option (Boot####, not put into BootOrder) for the OpenCore that does the wake, and
            records its number in /usr/local/etc/phxwake-bootnext. phxwaked then writes BootNext=#### whenever the
            system goes to sleep and deletes it again after the wake or an aborted sleep.
            The OpenCore is found on the boot disk's ESP: the one OpenCore.efi whose config.plist has
            Misc > Boot > HibernateMode other than None. Give it with --loader '\\EFI\\X\\OpenCore.efi' when there are several.
    status  shows what is set up.
    remove  deletes the boot option and the record.

Run as root. With OpenCore as the first boot option nothing of this is needed (OpenCore resumes by itself).
"""
import argparse, os, plistlib, re, struct, subprocess, sys, uuid

GLOBAL = '8BE4DF61-93CA-11D2-AA0D-00E098032B8C'
CONF = '/usr/local/etc/phxwake-bootnext'
DESC = 'macOS hibernation resume (nullarc)'
LOAD_OPTION_ACTIVE = 0x1


def sh(*a, check=True):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f'!! {" ".join(a)}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


def nvram_get(name):
    r = subprocess.run(['nvram', '-x', f'{GLOBAL}:{name}'], capture_output=True)
    if r.returncode:
        return None
    return next(iter(plistlib.loads(r.stdout).values()))


def nvram_set(name, data):
    sh('nvram', f'{GLOBAL}:{name}=' + ''.join(f'%{b:02x}' for b in data))


def nvram_del(name):
    sh('nvram', '-d', f'{GLOBAL}:{name}', check=False)


def boot_disk_esp():
    """The ESP on the disk that holds the booted APFS container."""
    info = plistlib.loads(subprocess.run(['diskutil', 'info', '-plist', '/'], capture_output=True).stdout)
    cont = info.get('APFSContainerReference') or info['ParentWholeDisk']
    stores = plistlib.loads(subprocess.run(['diskutil', 'apfs', 'list', '-plist', cont], capture_output=True).stdout)
    phys = stores['Containers'][0]['PhysicalStores'][0]['DeviceIdentifier']           # e.g. disk0s2
    whole = re.match(r'(disk\d+)', phys).group(1)
    lst = plistlib.loads(subprocess.run(['diskutil', 'list', '-plist', whole], capture_output=True).stdout)
    for d in lst['AllDisksAndPartitions']:
        for p in d.get('Partitions', []):
            if p.get('Content') == 'EFI':
                return p['DeviceIdentifier']
    sys.exit(f'!! no EFI partition on {whole}')


def partition(dev):
    i = plistlib.loads(subprocess.run(['diskutil', 'info', '-plist', dev], capture_output=True).stdout)
    bs = i.get('DeviceBlockSize', 512)
    num = int(re.search(r's(\d+)$', dev).group(1))
    return dict(num=num, start=i['PartitionMapPartitionOffset'] // bs, size=i['TotalSize'] // bs,
                guid=uuid.UUID(i['DiskUUID']), mount=i.get('MountPoint') or '')


def find_loader(dev):
    """The \\EFI\\*\\OpenCore.efi whose config.plist resumes from hibernation (HibernateMode != None)."""
    mounted_here = False
    mnt = partition(dev)['mount']
    if not mnt:
        sh('diskutil', 'mount', 'readOnly', dev); mounted_here = True
        mnt = partition(dev)['mount']
    try:
        found = []
        efi = os.path.join(mnt, 'EFI')
        for d in sorted(os.listdir(efi)):
            cfg = os.path.join(efi, d, 'config.plist')
            if not (os.path.isfile(os.path.join(efi, d, 'OpenCore.efi')) and os.path.isfile(cfg)):
                continue
            try:
                p = plistlib.load(open(cfg, 'rb'))
            except Exception:
                continue
            if p.get('Misc', {}).get('Boot', {}).get('HibernateMode', 'None') != 'None':
                found.append(f'\\EFI\\{d}\\OpenCore.efi')
        return found
    finally:
        if mounted_here:
            sh('diskutil', 'unmount', dev, check=False)


def load_option(part, path):
    hd = struct.pack('<BBH', 4, 1, 42) + struct.pack('<IQQ', part['num'], part['start'], part['size']) \
        + part['guid'].bytes_le + bytes([2, 2])                                            # GPT, GUID signature
    f = (path + '\0').encode('utf-16-le')
    fp = struct.pack('<BBH', 4, 4, 4 + len(f)) + f
    dp = hd + fp + bytes([0x7F, 0xFF, 4, 0])
    return struct.pack('<IH', LOAD_OPTION_ACTIVE, len(dp)) + (DESC + '\0').encode('utf-16-le') + dp


def parse_option(v):
    attr, fpl = struct.unpack_from('<IH', v)
    i = 6
    while v[i:i + 2] != b'\0\0':
        i += 2
    desc = v[6:i].decode('utf-16-le')
    dp = v[i + 2:i + 2 + fpl]
    paths, j = [], 0
    while j + 4 <= len(dp):                                 # walk the device path nodes; keep the file paths
        t, st, ln = struct.unpack_from('<BBH', dp, j)
        if ln < 4 or t == 0x7F:
            break
        if (t, st) == (4, 4):
            paths.append(dp[j + 4:j + ln].decode('utf-16-le').rstrip('\0'))
        j += ln
    return attr, desc, dp, paths


def boot_options():
    order = nvram_get('BootOrder') or b''
    seen = {f'{n:04X}' for n in struct.unpack(f'<{len(order) // 2}H', order)}
    seen |= {f'{n:04X}' for n in range(0x0000, 0x0010)} | {f'{n:04X}' for n in range(0x0080, 0x00A0)}
    out = {}
    for n in sorted(seen):
        v = nvram_get(f'Boot{n}')
        if v:
            out[n] = v
    return out


def cmd_setup(a):
    esp = boot_disk_esp(); part = partition(esp)
    if a.loader:
        loader = a.loader
    else:
        c = find_loader(esp)
        if len(c) != 1:
            sys.exit(f'!! {len(c)} OpenCore with hibernation on {esp}: {c or "none"}. Give one with --loader.')
        loader = c[0]
    opt = load_option(part, loader)
    opts = boot_options()
    num = next((n for n, v in opts.items() if v == opt), None)
    if num is None:
        num = a.num or next(f'{n:04X}' for n in range(0x0090, 0x0100) if f'{n:04X}' not in opts)
        if num in opts:
            sys.exit(f'!! Boot{num} exists and is something else')
        nvram_set(f'Boot{num}', opt)
        if nvram_get(f'Boot{num}') != opt:
            sys.exit(f'!! Boot{num} did not read back as written')
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    open(CONF, 'w').write(num + '\n')
    print(f'Boot{num} "{DESC}" -> {esp} (GPT {part["guid"]}) {loader}; recorded in {CONF}')
    print('Not in BootOrder: normal boots are unchanged. phxwaked sets BootNext on each sleep.')


def cmd_status(a):
    num = open(CONF).read().strip() if os.path.exists(CONF) else None
    print('record:', num or 'none')
    order = nvram_get('BootOrder')
    print('BootOrder:', ' '.join(f'{n:04X}' for n in struct.unpack(f'<{len(order) // 2}H', order)) if order else '-')
    nxt = nvram_get('BootNext')
    print('BootNext:', f'{struct.unpack("<H", nxt)[0]:04X}' if nxt else '-')
    for n, v in boot_options().items():
        attr, desc, dp, paths = parse_option(v)
        print(f'  Boot{n}{" *" if n == num else ""} attr={attr:#x} {desc!r} {paths}')


def cmd_remove(a):
    if os.path.exists(CONF):
        num = open(CONF).read().strip()
        v = nvram_get(f'Boot{num}')
        if v and parse_option(v)[1] == DESC:
            nvram_del(f'Boot{num}'); print(f'Boot{num} deleted')
        os.remove(CONF)
    nvram_del('BootNext')
    print('removed')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('setup'); s.add_argument('--loader'); s.add_argument('--num')
    sub.add_parser('status'); sub.add_parser('remove')
    a = ap.parse_args()
    if a.cmd != 'status' and os.geteuid():
        sys.exit('run as root')
    {'setup': cmd_setup, 'status': cmd_status, 'remove': cmd_remove}[a.cmd](a)


if __name__ == '__main__':
    main()
