#!/bin/sh
# Run from the Alpine 3.23 x86_64 ISO console, not from an installed system.
set -eu
umask 077

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root."
[ "$(uname -m)" = x86_64 ] || die "Only x86_64 is supported."
case "$(cat /etc/alpine-release 2>/dev/null)" in
    3.23.*) ;; *) die "Boot the Alpine 3.23 x86_64 ISO first." ;;
esac
[ -z "${SSH_CONNECTION:-}" ] || die "Use the VNC console for this stage."
exec 3<>/dev/tty || die "An interactive console is required."
case "$(awk '$2 == "/" {print $3}' /proc/mounts)" in
    tmpfs|rootfs) ;; *) die "This is not the expected Alpine ISO RAM environment." ;;
esac
[ ! -e /.dockerenv ] || die "Containers are not supported."
for flag in /sys/firmware/efi/efivars/SecureBoot-*; do
    [ -f "$flag" ] || continue
    [ "$(od -An -t u1 -j 4 -N 1 "$flag" | tr -d ' ')" != 1 ] || die "Disable Secure Boot first."
done

net_count=0
for net in /sys/class/net/*; do
    [ -e "$net/device" ] || continue
    net_count=$((net_count + 1))
done
[ "$net_count" = 1 ] || die "Exactly one hardware network interface is required."
setup-interfaces -a -r
ip -4 route show default | grep -q '^default ' || die "DHCP did not provide an IPv4 default route."

# APK signatures remain enabled, including during HTTPS bootstrap.
printf '%s\n' \
    'https://dl-cdn.alpinelinux.org/alpine/v3.23/main' \
    'https://dl-cdn.alpinelinux.org/alpine/v3.23/community' > /etc/apk/repositories
apk update
apk add alpine-base openssh util-linux e2fsprogs ca-certificates curl openssl cpio gzip
update-ca-certificates

disks=$(lsblk -bdnpo NAME,TYPE,SIZE | awk '$2 == "disk" && $3 > 0 && $1 !~ /^\/dev\/fd[0-9]+$/ {print $1}')
[ "$(printf '%s\n' "$disks" | grep -c '^/dev/')" = 1 ] || die "Exactly one system disk is required. Detach data disks first."
disk=$disks
[ "$(lsblk -dnro RO "$disk")" = 0 ] || die "The disk is read-only."
[ "$(blockdev --getsize64 "$disk")" -ge 2000000000 ] || die "At least 2,000,000,000 bytes of disk space is required."
boot_media=
mounted=$(lsblk -nrpo MOUNTPOINTS "$disk" | awk 'NF')
if [ -n "$mounted" ]; then
    boot_media=$(findmnt -nr -S "$disk" -o TARGET || :)
    [ -n "$boot_media" ] && [ "$mounted" = "$boot_media" ] || die "The target disk has mounted filesystems or active swap."
    [ "$(findmnt -nr -S "$disk" -o FSTYPE)" = iso9660 ] || die "Only the ISO boot-media mount may be released automatically."
    [ "$(findmnt -nr -M /.modloop -o FSTYPE)" = squashfs ] || die "The expected ISO module filesystem is missing."
    module_loop=$(findmnt -nr -M /.modloop -o SOURCE)
    [ "$(losetup -n -O BACK-FILE "$module_loop")" = "$boot_media/boot/modloop-virt" ] || die "The module image is not on the expected ISO boot media."
    [ "$(readlink /lib/modules)" = /.modloop/modules ] || die "Unexpected module directory; handle the boot media manually."
    command -v copy-modloop >/dev/null || die "The official copy-modloop helper is missing."
fi
if lsblk -nrpo TYPE "$disk" | grep -Eq 'lvm|crypt|raid|md'; then
    die "Active LVM, encryption, or RAID must be handled manually."
fi

boot_mode=BIOS
grub_package=grub-bios
if [ -d /sys/firmware/efi ]; then
    boot_mode=UEFI
    grub_package=grub-efi
fi
apk add "$grub_package"
if [ "$boot_mode" = UEFI ]; then apk add dosfstools; fi
setup-hostname -n alpine
hostname alpine
setup-timezone -z Asia/Shanghai
setup-ntp -c chrony
rc-update add networking boot
rc-update add hostname boot
printf '\nSet a unique root password. It will not be displayed.\n'
passwd root <&3 >&3 2>&3
root_hash=$(awk -F: '$1 == "root" {print $2}' /etc/shadow)
case "$root_hash" in '$'*) ;; *) die "A non-empty root password is required." ;; esac
unset root_hash

# Set the first global values so later includes cannot override them.
sed -i '/^[[:space:]]*PermitRootLogin[[:space:]]/d; /^[[:space:]]*PasswordAuthentication[[:space:]]/d' /etc/ssh/sshd_config
sed -i '1iPermitRootLogin yes\nPasswordAuthentication yes' /etc/ssh/sshd_config
ssh-keygen -A
sshd -t
rc-update add sshd default

# Check the complete target package set before touching the disk.
package_dir=$(mktemp -d /tmp/alpine-packages.XXXXXX)
cp /etc/apk/repositories "$package_dir/online-repositories"
cleanup_packages() {
    cp "$package_dir/online-repositories" /etc/apk/repositories
    rm -rf "$package_dir"
}
trap cleanup_packages EXIT
for repo in main community; do
    mkdir -p "$package_dir/$repo/x86_64"
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 \
        "https://dl-cdn.alpinelinux.org/alpine/v3.23/$repo/x86_64/APKINDEX.tar.gz" \
        -o "$package_dir/$repo/x86_64/APKINDEX.tar.gz"
done
apk fetch --recursive --output "$package_dir/main/x86_64" $(cat /etc/apk/world) alpine-base linux-virt "$grub_package"
for file in "$package_dir/main/x86_64/"*.apk; do
    ln "$file" "$package_dir/community/x86_64/${file##*/}"
done
printf '%s\n' "$package_dir/main" "$package_dir/community" > "$package_dir/offline-repositories"
mkdir "$package_dir/check"
apk --repositories-file "$package_dir/offline-repositories" fetch --recursive \
    --output "$package_dir/check" $(cat /etc/apk/world) alpine-base linux-virt "$grub_package"
rm -rf "$package_dir/check"

printf '\nBoot mode: %s\nALL DATA on this disk will be erased:\n' "$boot_mode"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS "$disk"
if [ -n "$boot_media" ]; then
    [ ! -e /lib/modules.tmp ] || die "A previous module copy exists; inspect it before retrying."
    module_kib=$(du -sk /.modloop/modules | awk '{print $1}')
    available_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
    [ "$available_kib" -ge "$((module_kib + 98304))" ] || die "Not enough available RAM to release the ISO boot media."
    printf 'Boot modules will be copied into RAM before releasing %s.\n' "$boot_media"
fi
printf '\nType ERASE %s to install Alpine: ' "$disk" >&3
IFS= read -r answer <&3 || die "No confirmation received."
[ "$answer" = "ERASE $disk" ] || die "Cancelled."

if [ -n "$boot_media" ]; then
    printf 'Copying boot modules into RAM and releasing the ISO boot media...\n'
    root_free_kib=$(df -Pk / | awk 'NR == 2 {print $4}')
    if [ "$root_free_kib" -lt "$((module_kib + 32768))" ]; then
        [ "$(awk '$2 == "/" {print $3}' /proc/mounts)" = tmpfs ] || die "The RAM root filesystem cannot hold the module copy."
        root_used_kib=$(df -Pk / | awk 'NR == 2 {print $3}')
        mount -o "remount,size=$((root_used_kib + module_kib + 32768))k" /
    fi
    DO_UMOUNT=1 modloop_media="$boot_media" copy-modloop
    [ -d "/lib/modules/$(uname -r)" ] || die "The RAM module copy is incomplete. Do not reboot."
fi
[ -z "$(lsblk -nrpo MOUNTPOINTS "$disk" | tr -d '[:space:]')" ] || die "The target disk is still in use. No partitioning was started."
wipefs --all "$disk"

# The official installer creates the matching partition table and bootloader.
export BOOTLOADER=grub ROOTFS=ext4 BOOT_SIZE=256
export ERASE_DISKS="$disk"
cp "$package_dir/offline-repositories" /etc/apk/repositories
setup-disk -m sys -s 256 "$disk"
sync

# Locate the installed root by its contents rather than partition number.
verify_dir=$(mktemp -d /mnt/alpine-check.XXXXXX)
cleanup() {
    mountpoint -q "$verify_dir/boot/efi" && umount "$verify_dir/boot/efi" || :
    mountpoint -q "$verify_dir/boot" && umount "$verify_dir/boot" || :
    mountpoint -q "$verify_dir" && umount "$verify_dir" || :
    rmdir "$verify_dir" 2>/dev/null || :
    cleanup_packages
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
found=no
for part in $(lsblk -lnpo NAME,FSTYPE "$disk" | awk '$2 == "ext4" {print $1}'); do
    mount "$part" "$verify_dir"
    if [ -f "$verify_dir/etc/alpine-release" ]; then found=yes; break; fi
    umount "$verify_dir"
done
[ "$found" = yes ] || die "Installed root filesystem not found. Do not reboot."
cp "$package_dir/online-repositories" "$verify_dir/etc/apk/repositories"
for target in /boot /boot/efi; do
    source=$(awk -v t="$target" '$2 == t {print $1}' "$verify_dir/etc/fstab")
    [ -n "$source" ] || continue
    mount "$source" "$verify_dir$target"
done
if [ "$boot_mode" = BIOS ]; then
    # setup-disk can mask a failed grub-install; check a direct installation.
    grub-install --target=i386-pc --boot-directory="$verify_dir/boot" "$disk"
fi
[ -s "$verify_dir/boot/grub/grub.cfg" ] || die "GRUB configuration is missing. Do not reboot."
[ -s "$verify_dir/boot/vmlinuz-virt" ] || die "The virtual kernel is missing. Do not reboot."
[ -s "$verify_dir/boot/initramfs-virt" ] || die "The initramfs is missing. Do not reboot."
[ -e "$verify_dir/etc/runlevels/default/sshd" ] || die "SSH startup is missing."
[ -x "$verify_dir/usr/sbin/sshd" ] || die "The installed SSH server is missing."
[ "$(awk -F: '$1 == "root" {print $2}' "$verify_dir/etc/shadow")" = "$(awk -F: '$1 == "root" {print $2}' /etc/shadow)" ] || die "The installed root password was not preserved."
if [ "$boot_mode" = UEFI ]; then
    [ -s "$verify_dir/boot/efi/EFI/boot/bootx64.efi" ] || die "The UEFI fallback loader is missing."
fi
cleanup
trap - EXIT
printf '\nAlpine installation and file checks completed.\n'
printf 'Detach the ISO or select boot from disk in the cloud console.\n'
printf 'Then run: reboot\n'
printf 'SSH login: root, port 22, using the password you just set.\n'
