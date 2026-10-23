#!/bin/sh
# Prepare the official Debian 13 installer from a disk-installed Alpine system.
set -eu
umask 077

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root."
[ "$(uname -m)" = x86_64 ] || die "Only x86_64 is supported."
case "$(cat /etc/alpine-release 2>/dev/null)" in
    3.23.*) ;; *) die "This script requires Alpine 3.23." ;;
esac
exec 3<>/dev/tty || die "An interactive terminal is required."
apk update
apk add curl ca-certificates openssl util-linux cpio gzip grub
update-ca-certificates
root_dev=$(findmnt -nro SOURCE /)
[ "$(lsblk -dnro TYPE "$root_dev")" = part ] || die "Boot the installed Alpine system first; LVM and RAID are unsupported."
[ "$(findmnt -nro FSTYPE /)" = ext4 ] || die "An ext4 root filesystem is required."
disk="/dev/$(lsblk -dnro PKNAME "$root_dev")"
disks=$(lsblk -bdnpo NAME,TYPE,SIZE | awk '$2 == "disk" && $3 > 0 && $1 !~ /^\/dev\/fd[0-9]+$/ {print $1}')
[ "$disks" = "$disk" ] || die "Exactly one system disk is required. Detach data disks first."
[ "$(blockdev --getsize64 "$disk")" -ge 2000000000 ] || die "At least 2,000,000,000 bytes of disk space is required."
memory_kib=$(awk '/MemTotal:/ {print $2}' /proc/meminfo)
# MemTotal excludes firmware and kernel reservations.
[ "$memory_kib" -ge 434176 ] || die "At least 424 MiB of OS-visible RAM is required; detected $memory_kib KiB."
if [ "$memory_kib" -lt 983040 ]; then
    [ "$(awk 'NR > 1 {total += $3} END {print total+0}' /proc/swaps)" -ge 262140 ] || die "Enable at least 256 MiB of swap before preparing the installer."
fi
[ -s /boot/grub/grub.cfg ] || die "GRUB is missing. This script does not repair broken bootloaders."
for flag in /sys/firmware/efi/efivars/SecureBoot-*; do
    [ -f "$flag" ] || continue
    [ "$(od -An -t u1 -j 4 -N 1 "$flag" | tr -d ' ')" != 1 ] || die "Disable Secure Boot first."
done
net_count=0
for net in /sys/class/net/*; do
    [ -e "$net/device" ] || continue
    net_count=$((net_count + 1))
    iface=${net##*/}
done
[ "$net_count" = 1 ] || die "Exactly one hardware network interface is required."
awk -v i="$iface" '$1 == "iface" && $2 == i && $3 == "inet" && $4 == "dhcp" {ok=1} END {exit !ok}' /etc/network/interfaces || die "The network must use DHCP."
ip -4 route show default | grep -q " dev $iface" || die "No IPv4 default route on the DHCP interface."
sectors=$(blockdev --getsz "$disk")
mac=$(cat "/sys/class/net/$iface/address")
boot_mode=BIOS
[ ! -d /sys/firmware/efi ] || boot_mode=UEFI
printf '\nTarget: %s\nBoot: %s\nNetwork: %s (DHCP)\n' "$disk" "$boot_mode" "$iface"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS "$disk"
printf '\nDebian will ERASE this disk after you boot its installer.\n'
printf 'Type ERASE %s to prepare the installer: ' "$disk" >&3
IFS= read -r answer <&3 || die "No confirmation received."
[ "$answer" = "ERASE $disk" ] || die "Cancelled."

tmp=$(mktemp -d /var/tmp/debian13.XXXXXX)
tty_state=$(stty -g <&3)
cleanup() {
    stty "$tty_state" <&3 2>/dev/null || :
    rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
printf 'Set the Debian root password (at least 12 characters): ' >&3
stty -echo <&3
IFS= read -r password <&3 || die "Password input failed."
printf '\nRepeat password: ' >&3
IFS= read -r repeated <&3 || die "Password input failed."
stty "$tty_state" <&3
printf '\n' >&3
[ "$password" = "$repeated" ] || die "Passwords do not match."
[ "${#password}" -ge 12 ] || die "Use at least 12 characters."
password_hash=$(printf '%s\n' "$password" | openssl passwd -6 -stdin)
unset password repeated

base=https://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images
fetch() { curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 20 --max-time 900 "$1" -o "$2"; }
fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS"
for file in linux initrd.gz; do
    fetch "$base/netboot/debian-installer/amd64/$file" "$tmp/$file"
    hash=$(awk -v p="./netboot/debian-installer/amd64/$file" '$2 == p {print $1}' "$tmp/SHA256SUMS")
    [ "${#hash}" = 64 ] || die "The official checksum is missing."
    printf '%s  %s\n' "$hash" "$tmp/$file" | sha256sum -c -
done
gzip -t "$tmp/initrd.gz"

mkdir "$tmp/payload"
cat > "$tmp/payload/preflight.sh" <<EOF
#!/bin/sh
set -eu
stop() { echo "INSTALL STOPPED: disk or hardware mismatch" >/dev/console; while :; do sleep 3600; done; }
# Low-memory netboot can omit the module containing VirtIO block drivers.
anna-install "scsi-modules-\$(uname -r)-di" || stop
depmod -a || stop
case "$disk" in /dev/vd*) modprobe virtio_blk || stop ;; esac
udevadm settle || stop
disks=\$(list-devices disk)
[ "\$disks" = "$disk" ] || stop
[ "\$(cat /sys/class/block/${disk##*/}/size)" = "$sectors" ] || stop
mode=BIOS
[ ! -d /sys/firmware/efi ] || mode=UEFI
[ "\$mode" = "$boot_mode" ] || stop
found=0
for net in /sys/class/net/*; do
    [ -e "\$net/device" ] || continue
    [ "\$(cat "\$net/address")" = "$mac" ] || stop
    found=\$((found + 1))
done
[ "\$found" = 1 ] || stop
EOF
cat > "$tmp/payload/finish.sh" <<'EOF'
#!/bin/sh
set -eu
mkdir -p /target/etc/ssh/sshd_config.d
printf '%s\n' 'PermitRootLogin yes' 'PasswordAuthentication yes' 'Port 22' > /target/etc/ssh/sshd_config.d/00-local-login.conf
in-target mkdir -p /run/sshd
in-target /usr/sbin/sshd -t
in-target systemctl enable ssh
in-target apt-get clean
EOF
cat > "$tmp/payload/preseed.cfg" <<EOF
d-i lowmem/low note
d-i preseed/early_command string anna-install scsi-modules-\$(uname -r)-di partman-auto && { [ ! -d /sys/firmware/efi ] || anna-install partman-efi; }
d-i debian-installer/language string en
d-i debian-installer/country string CN
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/choose_interface select auto
d-i netcfg/disable_autoconfig boolean false
d-i netcfg/dhcp_timeout string 60
d-i netcfg/get_hostname string debian
d-i netcfg/get_domain string localdomain
d-i mirror/country string manual
d-i mirror/protocol string https
d-i mirror/https/hostname string deb.debian.org
d-i mirror/https/directory string /debian
d-i mirror/https/proxy string
d-i mirror/suite string trixie
d-i passwd/root-login boolean true
d-i passwd/make-user boolean false
d-i passwd/root-password-crypted password $password_hash
d-i clock-setup/utc boolean true
d-i time/zone string Asia/Shanghai
d-i clock-setup/ntp boolean true
d-i partman/early_command string /bin/sh /preflight.sh
d-i partman-auto/disk string $disk
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select small-cloud
d-i partman-auto/expert_recipe string small-cloud :: 272 272 272 free \$iflabel{ gpt } \$reusemethod{ } method{ efi } format{ } . 272 272 272 linux-swap \$primary{ } method{ swap } format{ } . 1000 10000 -1 ext4 \$primary{ } \$bootable{ } method{ format } format{ } use_filesystem{ } filesystem{ ext4 } mountpoint{ / } .
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman/mount_style select uuid
d-i base-installer/install-recommends boolean false
d-i base-installer/kernel/image string linux-image-cloud-amd64
d-i apt-setup/cdrom/set-first boolean false
d-i apt-setup/services-select multiselect updates
d-i apt-setup/security_host string
d-i apt-setup/local0/repository string https://deb.debian.org/debian-security trixie-security main
d-i apt-setup/local0/comment string Debian security updates
d-i apt-setup/local0/source boolean false
tasksel tasksel/first multiselect
d-i pkgsel/include string openssh-server ca-certificates
d-i pkgsel/upgrade select none
popularity-contest popularity-contest/participate boolean false
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string $disk
d-i grub-installer/force-efi-extra-removable boolean true
d-i preseed/late_command string /bin/sh /finish.sh
d-i finish-install/reboot_in_progress note
d-i cdrom-detect/eject boolean false
EOF
unset password_hash
# Append a separate gzip-compressed cpio archive; Linux unpacks both members.
(cd "$tmp/payload" && printf '%s\n' preseed.cfg preflight.sh finish.sh | cpio -o -H newc > "$tmp/payload.cpio")
gzip -c "$tmp/payload.cpio" > "$tmp/payload.gz"
cat "$tmp/initrd.gz" "$tmp/payload.gz" > "$tmp/installer-initrd.gz"
gzip -t "$tmp/installer-initrd.gz"

boot_dir=/boot/debian13-installer
[ ! -e "$boot_dir" ] || die "An installer is already staged. See the cancellation steps in the guide."
[ "$(df -Pk /boot | awk 'NR == 2 {print $4}')" -ge 131072 ] || die "At least 128 MiB free in /boot is required."
mkdir -m 700 "$boot_dir"
cp "$tmp/linux" "$boot_dir/linux"
cp "$tmp/installer-initrd.gz" "$boot_dir/initrd.gz"
uuid=$(grub-probe --target=fs_uuid "$boot_dir")
kernel_path=$(grub-mkrelpath "$boot_dir/linux")
initrd_path=$(grub-mkrelpath "$boot_dir/initrd.gz")
case "$uuid" in ''|*[!a-zA-Z0-9-]*) die "Unexpected boot filesystem UUID." ;; esac
cat > /etc/grub.d/42_debian13_installer <<EOF
#!/bin/sh
cat <<'MENU'
menuentry 'Install Debian 13 - ERASE SYSTEM DISK' --id debian13-installer {
    insmod part_gpt
    insmod part_msdos
    insmod ext2
    search --no-floppy --fs-uuid --set=root $uuid
    linux $kernel_path auto=true priority=critical lowmem=1 ---
    initrd $initrd_path
}
MENU
EOF
chmod 700 /etc/grub.d/42_debian13_installer
cp -p /boot/grub/grub.cfg /boot/grub/grub.cfg.before-debian13
grub-mkconfig -o "$tmp/grub.cfg"
grub-script-check "$tmp/grub.cfg"
grep -q 'id debian13-installer' "$tmp/grub.cfg" || die "The installer entry is missing."
cp "$tmp/grub.cfg" /boot/grub/grub.cfg.new
mv /boot/grub/grub.cfg.new /boot/grub/grub.cfg
sync
printf '\nREADY: installer files and GRUB configuration passed local checks.\n'
printf 'Open VNC, detach the ISO, then run these commands to start installation:\n\n'
printf "grub-reboot debian13-installer && reboot\n\n"
printf 'Installation erases %s. Watch VNC for progress or errors.\n' "$disk"
printf 'After Debian finishes: SSH as root on port 22 with your new password.\n'
printf 'The SSH host key will change. Verify the new key through VNC.\n'
