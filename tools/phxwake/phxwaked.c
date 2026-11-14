// phxwaked: runs /usr/local/libexec/phxwake.sh after every system wake. On this machine a system sleep is a
// hibernation (no S3), so the Radeon 780M comes back in its power-on state and the GPU bring-up has to run again
// before the held submissions and the display can continue. Root LaunchDaemon local.phxwake.
//
// A wake from hibernation is a cold boot. When another boot loader (Limine, GRUB, rEFInd) comes before OpenCore, its
// menu would show up on every wake. If phxbootnext.py setup recorded a firmware boot option for the resuming OpenCore
// in /usr/local/etc/phxwake-bootnext, the UEFI one-shot BootNext is set to it as the system goes to sleep (only when
// PhxSleepPolicy makes that sleep a hibernation), so the firmware starts that OpenCore once and drops the variable.
// It is deleted again after the wake and when the sleep is called off.
#include <IOKit/IOKitLib.h>
#include <IOKit/IOMessage.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <CoreFoundation/CoreFoundation.h>
#include <sys/sysctl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static io_connect_t gRoot;
static const char *gScript = "/usr/local/libexec/phxwake.sh";
static const char *gBootNextConf = "/usr/local/etc/phxwake-bootnext";
#define BOOTNEXT "8BE4DF61-93CA-11D2-AA0D-00E098032B8C:BootNext"

static void stamp(const char *what) {
    char t[32]; time_t now = time(NULL); strftime(t, sizeof t, "%F %T", localtime(&now));
    fprintf(stderr, "%s phxwaked: %s\n", t, what); fflush(stderr);
}

static int nvramSet(CFStringRef key, CFTypeRef value) {
    io_registry_entry_t o = IORegistryEntryFromPath(kIOMainPortDefault, "IODeviceTree:/options");
    if (!o) return -1;
    kern_return_t kr = IORegistryEntrySetCFProperty(o, key, value);
    IOObjectRelease(o);
    return kr == KERN_SUCCESS ? 0 : (int)kr;
}

// The recorded Boot#### (four hex digits), or -1.
static int bootNextOption(void) {
    FILE *f = fopen(gBootNextConf, "r");
    if (!f) return -1;
    char b[16] = {0}; unsigned n = 0;
    int ok = fgets(b, sizeof b, f) && sscanf(b, "%4x", &n) == 1;
    fclose(f);
    return ok ? (int)n : -1;
}

static int hibernates(void) {
    int v = 0; size_t l = sizeof v;
    return sysctlbyname("debug.phxsleep_installed", &v, &l, NULL, 0) == 0 && v == 1;
}

static void bootNextSet(void) {
    int n = bootNextOption();
    if (n < 0 || !hibernates()) return;
    UInt8 le[2] = { (UInt8)(n & 0xff), (UInt8)(n >> 8) };
    CFDataRef d = CFDataCreate(NULL, le, 2);
    int r = nvramSet(CFSTR(BOOTNEXT), d);
    CFRelease(d);
    if (!r) r = nvramSet(CFSTR("IONVRAM-FORCESYNCNOW-PROPERTY"), CFSTR(BOOTNEXT));
    char m[64]; snprintf(m, sizeof m, "BootNext=%04X %s", n, r ? "FAILED" : "set");
    stamp(m);
}

static void bootNextClear(const char *why) {
    if (bootNextOption() < 0) return;
    nvramSet(CFSTR(kIONVRAMDeletePropertyKey), CFSTR(BOOTNEXT));
    char m[64]; snprintf(m, sizeof m, "BootNext cleared (%s)", why);
    stamp(m);
}

static void power(void *ref, io_service_t svc, natural_t type, void *arg) {
    (void)ref; (void)svc;
    switch (type) {
    case kIOMessageCanSystemSleep:
        IOAllowPowerChange(gRoot, (long)arg);
        break;
    case kIOMessageSystemWillSleep:
        bootNextSet();                                  // the kext holds GPU submissions; nothing else to do here
        IOAllowPowerChange(gRoot, (long)arg);
        break;
    case kIOMessageSystemWillNotSleep:
        bootNextClear("sleep called off");
        break;
    case kIOMessageSystemHasPoweredOn:
        stamp("system has powered on");
        bootNextClear("woke");
        system(gScript);
        break;
    default: break;
    }
}

int main(int argc, char **argv) {
    if (argc > 1) gScript = argv[1];
    IONotificationPortRef port; io_object_t notifier;
    gRoot = IORegisterForSystemPower(NULL, &port, power, &notifier);
    if (!gRoot) { stamp("IORegisterForSystemPower failed"); return 1; }
    CFRunLoopAddSource(CFRunLoopGetCurrent(), IONotificationPortGetRunLoopSource(port), kCFRunLoopDefaultMode);
    int n = bootNextOption();
    char m[96];
    if (n >= 0) snprintf(m, sizeof m, "waiting for wakes (BootNext -> Boot%04X on hibernation)", n);
    else snprintf(m, sizeof m, "waiting for wakes");
    stamp(m);
    CFRunLoopRun();
    return 0;
}
