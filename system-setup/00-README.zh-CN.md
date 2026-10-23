# Alpine → Debian 13 安装指南

适用于从 `alpine-virt-3.23.2-x86_64.iso` 启动的阿里云实例，采用精简系统的小盘安装方案。所有脚本和粘贴命令均为纯英文 ASCII、LF 换行。针对 VNC 输入长度限制，两个阶段均已分段，每个命令文件不超过 1800 字符。每个阶段需依次粘贴 4 段数据，再粘贴校验执行命令。

监控工具的部署与配置见 [项目 README](../README.md)。系统安装独立于监控服务，可按需使用。

此安装方案面向精简系统。若要在该机器上部署 Docker，需额外预留镜像、日志及更新空间；2 GB 系统盘不适合作为 Docker 部署容量。

## 环境要求

- x86_64，BIOS 或 UEFI，关闭 Secure Boot。
- 单块可擦除系统盘，至少 2,000,000,000 字节（十进制 2 GB，约 1.86 GiB）；目标配置为 2 核、512 MiB 内存、2 GB 磁盘。
- 单块硬件网卡，IPv4 DHCP。多盘、多网卡、静态网络、LVM/RAID/加密根分区不在本流程范围内。
- 可以访问阿里云 VNC；安全组允许你的管理地址访问 TCP 22。
- 系统盘上的现有系统、分区、文件都会删除。先备份或创建快照。存在数据盘时，先按云平台要求卸载数据盘；不要为通过检查删除数据。

支持独立虚拟光驱中的 ISO，也支持将原始 ISO 写入这块系统盘后启动的情况。后一种情况会显示 `/dev/vda` 为 `iso9660` 并挂载到 `/media/vda`；模块镜像仍依赖该盘，不能直接擦除或强制卸载。脚本只在整盘 ISO 挂载与当前模块来源一致时识别此情形，并在最终确认后调用 Alpine 官方 `copy-modloop`，将模块复制到内存并正常释放启动介质。其他挂载分区和活动 swap 仍会被拒绝。

第一阶段将 Alpine 安装到硬盘并启用 SSH；第二阶段使用 Debian 官方网络安装器重装 Debian 13（trixie）。最终用户为 `root`，端口为 `22`。两个阶段分别设置密码，没有公共默认密码。Debian 密码至少 12 个字符，支持特殊字符，输入不回显。

## 分段输入与校验

`FAILED` 表示文件不完整或内容发生变化；校验后的 `&&` 已阻止安装脚本执行。不要绕过校验直接运行该文件。

如果终端仍显示 `>`，先按 Ctrl+C 回到普通命令提示符。若已显示 `localhost:~#`，可重新开始当前阶段：从对应 `CHUNKS` 文件夹的 `01-PART.txt` 开始逐个粘贴，最后运行 `99-CHECK-AND-RUN.txt`。重新粘贴会覆盖对应内容，无须手动删除文件或重启服务器。不要绕过挂载检查或强制卸载模块文件系统。

分段使用压缩后的 Base64 数据，避免通过 VNC 直接输入脚本里的美元符、反斜杠、反引号和复杂引号。每段独立保存并校验，重复粘贴同一段不会重复追加；最后完整校验通过才执行原脚本。需要每次等待普通命令提示符重新出现后再粘贴下一段。

## 第一阶段：安装 Alpine

1. 从指定 ISO 的 Alpine 环境启动，可以使用独立虚拟光驱，也可以是已经将原始 ISO 写入系统盘的环境。VNC 中以 `root` 登录；原版 ISO 初始没有密码。
2. 打开电脑上的 `01-INIT-CHUNKS` 文件夹，按文件名前的数字顺序逐个打开 `.txt`，每次只复制一个文件的全文到 VNC 并回车。每个 `PART` 都必须显示 `OK`；失败时重贴同一个文件即可覆盖修复。最后再粘贴 `99-CHECK-AND-RUN.txt`，它会重建原脚本、校验完整 SHA-256 并执行。不要一次复制整个文件夹的内容，也不要添加 Markdown 标记。
3. 脚本配置 DHCP、从 Alpine 官方 CDN 安装工具，然后提示设置 Alpine root 密码。接着在擦盘前下载完整目标软件包和官方签名索引，并验证本地副本。实际写盘使用本地软件源，完成后恢复官方在线软件源。硬盘安装创建 256 MiB 引导分区和 256 MiB swap，其余分给 ext4 根分区。UEFI 模式中该引导分区挂载为 `/boot/efi`，BIOS 模式中挂载为 `/boot`。
4. 核对显示的目标磁盘与 BIOS/UEFI 模式。确认允许清空后，输入脚本要求的确认文本。例如目标是 `/dev/vda` 时：

```sh
ERASE /dev/vda
```

5. 如果 ISO 就在目标系统盘上，确认后会先显示 `Copying boot modules into RAM and releasing the ISO boot media...`，随后正常卸载 `/.modloop` 和 ISO 挂载，再分区。复制前会检查物理内存余量；必要时按需调整 RAM 根文件系统的容量上限，服务器的物理内存配置不会改变。复制或卸载失败会停止，不使用强制或延迟卸载。解除占用后才清除旧 ISO / 分区签名并安装；BIOS 的 GRUB 安装会单独执行并检查退出状态。只有看到 `Alpine installation and file checks completed.` 才继续。出现 `ERROR` 时不要重启，保留报错并排查。
6. 使用独立虚拟光驱时，在云控制台卸载 ISO，或设为系统盘优先启动。如果 ISO 原本写在系统盘上，它已被替换为 Alpine，不需要另找虚拟光驱卸载。确认看到完成提示后执行：

```sh
reboot
```

7. 启动后可继续使用 VNC，或者在 Windows PowerShell 输入下面的命令，通过实例公网 IP 连接：

```powershell
$serverIp = Read-Host 'Server public IP'
ssh "root@$serverIp"
```

使用刚设置的 Alpine 密码。没有公网 IP 时，使用能够访问私网的网络或继续使用 VNC。

## 第二阶段：安装 Debian

必须已经完成第一阶段，并从系统盘重新启动；不能停留在 ISO 内存系统里。

1. 打开 `02-DEBIAN-CHUNKS` 文件夹，按数字顺序每次粘贴一个 `.txt` 文件，确认每个 `PART` 显示 `OK`，最后粘贴 `99-CHECK-AND-RUN.txt`。也可通过 SSH 上传 `02-install-debian13.sh` 到服务器后执行 `sh /root/02-install-debian13.sh`。
2. 脚本检查系统盘、ext4 根分区、DHCP、GRUB 和内存，并显示目标磁盘。内存判断使用 Linux 的 `MemTotal`（扣除内核和固件保留空间后的总量），最低为 424 MiB；小内存实例还需至少 256 MiB 活动 swap。
3. 输入要求的 `ERASE` 确认，然后设置并重复输入 Debian root 密码。
4. 脚本经 HTTPS 下载官方安装内核和 initrd，核对 SHA-256，加入安装配置，再生成独立 GRUB 菜单项。原 Alpine 菜单项保留。
5. 只有看到 `READY: installer files and GRUB configuration passed local checks.` 才继续。准备阶段不会自动重启，不会将安装器设为永久默认项。
6. 打开 VNC，确认 ISO 已卸载，然后执行：

```sh
grub-reboot debian13-installer && reboot
```

此后安装器会擦除系统盘，SSH 会断开。在 VNC 中观察安装过程。安装器会再次检查磁盘名称、容量、网卡 MAC 和启动模式；不匹配时停止分区。

安装使用 Debian 官方安装器和专门的小盘分区方案：约 272 MB swap，UEFI 另有约 272 MB EFI 分区，其余作为 ext4 根分区；BIOS 不创建 EFI 分区。普通软件源和安全更新源都使用 Debian 官方 HTTPS 地址。只安装基础系统、OpenSSH 和 CA 证书，使用官方 `linux-image-cloud-amd64` 云内核，不选择标准工具任务或桌面，不安装推荐包，也不在安装阶段额外执行整机升级。安装完成时清理软件包下载缓存。通过 `lowmem=1` 启用文本低内存模式，引导安装由 Debian 安装器处理。安装成功后自动重启，所需时间取决于网络和软件源。

如果显示 `Downloading a file failed` 或 `Download installer components` 下载失败，先确认网络可用，再在 VNC 选择 `Retry`；这会重新下载，不会跳过软件包校验。如果基础系统阶段也因网络失败，保留报错并确认网络恢复后，可从安装器主菜单重新选择 `Install the base system`，利用已有缓存重试；不要跳过基础系统或引导安装。反复失败时保留错误并检查网络或镜像访问。DHCP 或磁盘操作报错也应在 VNC 中查看提示；不要盲目跳过错误或在分区期间强制断电。若重启仍进入 Alpine，可在 VNC 的 GRUB 菜单中手动选择 `Install Debian 13 - ERASE SYSTEM DISK`。

## 登录与检查

先在 VNC 中用 root 和第二阶段密码登录，执行：

```sh
cat /etc/os-release
ip -4 addr
ip -4 route
free -m
swapon --show
systemctl is-active ssh
ss -lntp | grep ':22 '
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

确认系统版本、网络和 SSH 正常。最后一条显示新的 SSH 主机密钥指纹。重装后密钥会改变；在电脑上连接时核对 VNC 中的指纹，再删除旧记录并重新连接：

```powershell
$serverIp = Read-Host 'Server public IP'
ssh-keygen -R "$serverIp"
ssh "root@$serverIp"
```

2 GB 磁盘空间有限，安装成功不代表适合继续安装 Docker、面板或大量应用。安装后先运行 `df -h / /boot/efi`（BIOS 仅需 `df -h /`）确认余量。后续内核和系统更新仍需临时空间，避免让日志和软件包缓存占满磁盘。

脚本启用 root 密码登录；使用强密码并通过安全组限制管理来源地址，后续可自行切换为密钥登录。

## 首次系统更新

完成首次登录并确认网络、swap 和剩余空间后，在 Debian 中执行。官方安装介质中的初始内核可能早于当前安全更新；本步骤会更新已安装的软件，并允许安装内核更新所需的新包。先核对 apt 显示的空间需求，再输入 `y` 确认。

```sh
apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update
apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 --with-new-pkgs upgrade
apt-get clean
reboot
```

重启后重新登录，再检查内核、剩余空间、swap 和 SSH：

```sh
uname -r
df -m /
swapon --show
systemctl is-active ssh
```

保留 `linux-image-cloud-amd64` 元包，以便后续接收云内核更新。2 GB 空间有限，更新前应检查余量；不要直接删除当前运行内核的文件。

## 尚未启动安装器时取消

仅限仍在 Alpine、尚未开始 Debian 分区的阶段。依次执行：

```sh
grub-editenv /boot/grub/grubenv unset next_entry
rm -f /etc/grub.d/42_debian13_installer
grub-mkconfig -o /boot/grub/grub.cfg
```

确认上面的命令成功后再执行：

```sh
rm -rf /boot/debian13-installer
```

之后可以重新运行第二阶段。分区开始后，取消不能恢复旧数据，只能使用之前的快照或备份。

## 文件说明

| 文件 | 用途 |
|---|---|
| `01-INIT-CHUNKS/` | 第一阶段按顺序逐个粘贴的命令文件 |
| `02-DEBIAN-CHUNKS/` | 第二阶段按顺序逐个粘贴的命令文件 |
| `01-init-alpine.sh` | Alpine 初始化源脚本 |
| `02-install-debian13.sh` | Debian 安装准备源脚本 |
| `SHA256SUMS` | 安装文件校验值 |

修改源脚本后，在项目根目录运行 `python scripts/package_setup.py`，重新生成分段命令和校验清单。使用 `python scripts/package_setup.py --check` 检查分段内容与源脚本是否一致。

参考：[Debian 13 自动安装文档](https://www.debian.org/releases/trixie/amd64/apbs04.en.html)、[官方安装器校验文件](https://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/SHA256SUMS)。
