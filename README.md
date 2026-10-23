# 阿里云 ECS 保活监控

在服务器上通过 Docker Compose 持续监控阿里云 ECS，依据账号共享 CDT 流量池用量自动启停实例，并通过飞书发送通知和日报。支持多账号、多地域，使用 Python 3.14。

## 功能

- 每五分钟检查实例状态和流量用量。
- 分别管理中国内地与非内地共享池，支持自定义阈值。
- 用量低于阈值时启动实例，达到阈值时停止实例。
- 支持按实例暂停自动控制。
- 使用飞书 Card 2.0 发送启停通知、异常告警和每日汇总。

> 自动控制会启动人工关机的实例。维护前，请将对应实例的 `paused` 设为 `true`。调度、用量更新和实际关机存在延迟，阈值控制不能保证零超额费用。

## 部署

需要安装 Docker Engine 和 Docker Compose 插件。请使用一台持续在线、不会被本监控关停的服务器，并确保能够访问阿里云 API、飞书和镜像源。

### 1. 配置阿里云权限

为每个阿里云账号创建用于监控的 RAM 用户，并保存其 AccessKey ID 和 AccessKey Secret。

进入 **RAM 控制台 → 权限管理 → 权限策略 → 创建权限策略 → 脚本编辑**，粘贴以下 JSON：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["cdt:ListCdtInternetTraffic", "ecs:DescribeInstances"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["ecs:StartInstance", "ecs:StopInstance"],
      "Resource": [
        "acs:ecs:ap-northeast-1:REPLACE_ACCOUNT_ID:instance/i-REPLACE_INSTANCE_ID"
      ]
    }
  ]
}
```

- `REPLACE_ACCOUNT_ID`：实例所属阿里云主账号的数字 UID，可在账号信息中查看。
- `ap-northeast-1`：替换为实例所在地域 ID。
- `i-REPLACE_INSTANCE_ID`：替换为 ECS 实例 ID。多个实例需在 `Resource` 数组中分别列出 ARN。

将策略命名为 `ECS-Monitor`，名称仅使用英文字母、数字和中划线 `-`。创建后，在对应 RAM 用户的权限管理页面添加该自定义策略，授权范围选择整个云账号；实例启停范围由 JSON 中的 `Resource` 限定。

这里的 `Action` 是自定义策略中的操作名。授权页面按策略名称搜索，创建后选择 `ECS-Monitor` 即可。详细操作见 [阿里云自定义权限策略文档](https://help.aliyun.com/zh/ram/create-a-custom-policy)。

### 2. 配置飞书机器人

在飞书群聊中添加自定义机器人，安全设置选择关键词 **ECS Monitor**，保存 Webhook 地址。若配置 IP 白名单，需允许 部署服务器的公网出口地址。

### 3. 准备配置

下载项目并进入目录：

```sh
git clone https://github.com/Miafetta/aliyun-ecs-monitor.git
cd aliyun-ecs-monitor
umask 077
touch accounts.json webhook.txt
```

将以下账号配置写入 `accounts.json`；将飞书 Webhook 地址单独写入 `webhook.txt`，一行填写完整地址。配置文件通过 Compose secrets 只读挂载，不会打包进镜像。

账号配置示例：

```json
[
  {
    "name": "account-1",
    "access_key_id": "REPLACE_ACCESS_KEY_ID",
    "access_key_secret": "REPLACE_ACCESS_KEY_SECRET",
    "limits_gb": {"mainland": 18, "overseas": 180},
    "instances": [
      {
        "id": "i-exampleTokyo",
        "region": "ap-northeast-1",
        "name": "tokyo",
        "paused": false
      },
      {
        "id": "i-exampleHongkong",
        "region": "cn-hongkong",
        "name": "hongkong",
        "paused": true
      }
    ]
  }
]
```

将 AccessKey 占位符和示例实例 ID 替换为实际值。示例中的香港实例已暂停自动控制，启用前将其 `paused` 改为 `false`。**AccessKey ID 填入 `access_key_id`，主账号 UID 仅用于权限策略的资源 ARN。** 这两个文件包含凭据，已加入 Git 忽略规则；请限制服务器上的读取权限。

如需调整日报时间，在项目目录新建 `.env`：

```dotenv
REPORT_HOUR=5
```

`REPORT_HOUR` 取值为 `0`～`23`，使用北京时间，默认 `5`。宿主机时区不影响日报时间。

### 4. 构建与启动

先构建镜像，再运行只读诊断：

```sh
docker compose build
docker compose run --rm monitor --diagnose
```

诊断模式只查询，不启停实例、不发送通知、不保存状态。确认账号、实例和用量正确后启动：

```sh
docker compose up -d
docker compose logs --tail=100 -f monitor
```

启动后立即检查一次，之后约每五分钟检查一次；每轮串行执行，不重叠。如果一轮超过五分钟，会在完成后继续下一轮。单轮失败会记录日志并在后续轮次重试。Docker 服务启动后，容器按 `unless-stopped` 策略自动恢复；主动停止的服务需手动启动。

### 日常管理

```sh
# 查看运行状态与日志
docker compose ps
docker compose logs --tail=100 monitor

# 检查配置（可在服务运行时执行）
docker compose run --rm monitor --diagnose

# 修改配置后重新创建容器
docker compose up -d --force-recreate

# 更新程序
git pull --ff-only
docker compose up -d --build

# 停止服务，保留状态数据
docker compose down
```

凭据文件每轮重新读取，原地修改后会在下一轮生效；编辑器替换文件或修改 `.env` 后，应重新创建容器以确保更新挂载。服务不需要开放入站端口。

## 配置说明

每个阿里云账号对应一个配置条目，将该账号全部受监控实例放入同一个 `instances` 数组。

| 字段 | 说明 |
|---|---|
| `name` | 唯一且稳定的账号别名，修改后会重置该账号的通知标识 |
| `access_key_id` / `access_key_secret` | RAM 用户的 AccessKey |
| `limits_gb.mainland` | 中国内地共享池阈值，默认 `18` GB |
| `limits_gb.overseas` | 非内地共享池阈值，默认 `180` GB，包含香港 |
| `instances[].id` | ECS 实例 ID |
| `instances[].region` | 实例地域 ID |
| `instances[].name` | 可选的实例名称，默认使用实例 ID |
| `instances[].paused` | 是否暂停自动控制，JSON 布尔值，默认 `false` |

`limits_gb` 及其中任一阈值均可省略。未知字段会导致配置被拒绝；支持的地域见 [监控程序](src/monitor.py) 中的 `REGIONS`，未知地域会阻止对应账号的控制。

## 运行规则

共享池统计包含该账号对应地域池内的全部资源用量，启停操作仅针对配置中的实例。用量低于阈值时启动未暂停且已停止的实例；达到或超过阈值时停止未暂停且正在运行的实例。用量计算假设 CDT 返回当月累计字节数，按 `1 GB = 1024³` 字节换算。

用量查询异常、响应不完整、地域无法分类，或任一未暂停实例状态查询失败时，本轮跳过该账号的所有控制并告警，其他账号继续处理。

启停请求被接受后发送“请求已提交”，后续运行确认目标状态后发送“操作已完成”。中间状态不重复操作，超过 15 分钟未完成时告警并重新检查。通知失败不会阻塞必要的实例控制。

普通错误通知冷却一小时，超限通知冷却一天，仅发送成功才记录冷却。日报在北京时间指定小时之后首次成功运行时发送，每天一次；当轮查询、控制或通知失败时顺延。

运行状态保存在 `monitor-state` 数据卷中，重建或重启容器会保留状态。不要执行 `docker compose down -v`，除非确定要清空状态。请备份数据卷；状态丢失可能导致重复通知或重复提交操作，卡片部分发送失败后的重试也可能重复通知。同一实例只应由一个监控服务管理；共享状态目录的写入进程通过文件锁互斥。容器运行状态不等于查询成功，请定期查看日志中的 `Check completed: ok` 或 `failed`。

## 本地使用

使用 Python 3.14 安装依赖并预览通知卡片：

```sh
python -m pip install -r requirements.txt
python -m src.monitor --preview
```

预览使用示例数据，不访问网络。配置 `ALIYUN_ACCOUNTS_FILE` 指向账号 JSON 文件后，可运行只读诊断：

```sh
python -m src.monitor --diagnose
```

本地持续运行使用 `python -m src.monitor --serve`，文件锁互斥适用于 Linux。账号和飞书分别通过 `ALIYUN_ACCOUNTS_FILE`、`FEISHU_WEBHOOK_FILE` 读取；未指定文件时，也支持 `ALIYUN_ACCOUNTS_JSON`、`FEISHU_WEBHOOK_URL` 环境变量。文件配置优先。

诊断日志包含账号别名、实例信息和汇总用量。请勿将凭据写入命令历史或提交到仓库。

## 系统安装

项目附带可选的 [Alpine → Debian 13 安装工具](system-setup/00-README.zh-CN.md)，支持通过控制台分段输入命令安装精简系统。

适用于 x86_64、单盘、单硬件网卡、IPv4 DHCP、BIOS 或 UEFI，要求关闭 Secure Boot。最低磁盘容量为 2,000,000,000 字节；Debian 准备阶段要求系统可见内存至少 424 MiB，低内存环境需至少 256 MiB 活动 swap。

**安装会清空目标磁盘。请先备份数据、创建快照，并确认独立 VNC 控制台可用。** 操作步骤及完整环境要求见安装说明。

## 故障排查

容器、权限、通知和安装问题见 [故障排查](docs/troubleshooting.md)。

## 致谢

感谢 [10000ge10000/aliyun_monitor](https://github.com/10000ge10000/aliyun_monitor) 和 [hizzt/aliyun-monitor](https://github.com/hizzt/aliyun-monitor) 提供的功能思路与使用经验。

## 许可证

[MIT](LICENSE)
