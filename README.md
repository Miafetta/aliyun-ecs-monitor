# 阿里云 ECS 保活监控

在服务器上通过 Docker Compose 持续监控阿里云 ECS，依据账号共享 CDT 流量池用量自动启停实例，并通过飞书发送通知和日报。支持多账号、多地域，使用 Python 3.14。

## 功能

| 功能 | 说明 |
|---|---|
| 流量监控 | 每五分钟查询账号 CDT 用量，分别统计中国内地与非内地共享池 |
| 自动启停 | 低于阈值时启动已停止实例，达到阈值时停止运行实例 |
| 多账号、多实例 | 每个账号单独配置凭据与阈值，账号之间独立处理 |
| 维护暂停 | 通过 `paused` 暂停指定实例的自动控制 |
| 飞书通知 | 发送启停请求、完成确认、异常告警和北京时间日报 |
| 持久化状态 | 保存待确认动作、通知冷却和日报日期，容器重启后继续使用 |

> 自动控制会启动人工关机的实例。维护前，请将对应实例的 `paused` 设为 `true`。调度、用量更新和实际关机存在延迟，阈值控制不能保证零超额费用。

## 部署

需要安装 Docker Engine 和 Docker Compose 插件。请使用一台持续在线、不会被本监控关停的服务器，并确保能够访问阿里云 API、飞书和镜像源。

部署前准备以下内容：

| 配置项 | 获取位置 | 用途 |
|---|---|---|
| RAM 用户 AccessKey ID / Secret | RAM 控制台 → 用户详情 → 创建 AccessKey | 查询用量、查询和启停实例 |
| 阿里云主账号 UID | 阿里云账号信息 | 填写权限策略的资源 ARN |
| ECS 实例 ID 与地域 ID | ECS 控制台 → 实例列表 → 实例详情 | 指定受监控实例，见[地域对照表](#地域对照表) |
| 飞书 Webhook | 飞书群聊 → 自定义机器人配置 | 发送通知与日报 |

部署文件清单：

| 文件 | 内容 | 是否必填 |
|---|---|---|
| `accounts.json` | 日报时间、账号凭据、实例列表和流量阈值 | 是 |
| `webhook.txt` | 飞书机器人 Webhook，单行完整地址 | 是 |

### 1. 配置阿里云权限

1. 使用主账号或 RAM 管理员进入 [RAM 控制台](https://ram.console.aliyun.com/)，创建用于监控的 RAM 用户。
2. 进入该用户详情，创建 AccessKey，保存生成的 AccessKey ID 和 AccessKey Secret。Secret 仅在创建时显示，操作说明见[创建 AccessKey](https://help.aliyun.com/zh/ram/user-guide/accesskey-pair-management/)。
3. 为该用户创建并绑定下方权限策略。每个阿里云账号分别操作一次。

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

1. 打开接收通知的飞书群聊，在群设置的机器人管理中添加自定义机器人。
2. 安全设置选择关键词，填写 **ECS Monitor**，保存生成的 Webhook 地址。
3. 将地址写入 `webhook.txt`。如启用 IP 白名单，允许部署服务器的公网出口地址。

卡片标题包含 `ECS Monitor`，告警与日报均发送到这个群。

### 3. 准备配置

从 [ECS 控制台](https://ecs.console.aliyun.com/)的实例详情复制实例 ID 和地域 ID。配置中的 `region` 与 RAM 策略 ARN 中的地域必须一致，并与实例实际所在地相符。

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
{
  "report_hour": 5,
  "accounts": [
    {
      "name": "account-1",
      "access_key_id": "REPLACE_ACCESS_KEY_ID",
      "access_key_secret": "REPLACE_ACCESS_KEY_SECRET",
      "limits_gb": {
        "mainland": 18,
        "overseas": 180
      },
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
}
```

将 AccessKey 占位符和示例实例 ID 替换为实际值。示例中的香港实例已暂停自动控制，启用前将其 `paused` 改为 `false`。**`access_key_id` 填写 AccessKey ID；权限策略 ARN 中的账号段填写主账号 UID。** 这两个文件包含凭据，已加入 Git 忽略规则；请限制服务器上的读取权限。

日报时间由 `accounts.json` 顶层的 `report_hour` 设置，取值为 `0`～`23` 的 JSON 整数，默认 `5`，即北京时间 05:00。宿主机时区不影响日报时间；所有账号汇总为一份日报，每天一次。

调整该值后，程序在下一轮读取配置时生效；当天已成功发送日报时不会再次发送。旧版仅包含账号数组的配置仍可使用，日报时间为默认的 05:00。需要自定义时间时，请改为上述包含 `report_hour` 和 `accounts` 的对象格式。

### 4. 构建与启动

先构建镜像，再运行只读诊断和通知测试：

```sh
docker compose build
docker compose run --rm monitor --diagnose
docker compose run --rm monitor --test-notification
```

只读诊断查询账号、实例和用量，不启停实例、不发送通知。通知测试依次向飞书群发送以下卡片，标题均带“通知测试”：

| 卡片 | 内容 |
|---|---|
| 查询异常、共享池达到阈值 | 模拟查询失败和用量达到阈值的场景 |
| 请求已提交、操作已完成、操作等待超时、控制请求失败 | 分别展示启动、停止两种操作的通知，共八张 |
| 每日报告 | 展示账号用量、实例状态及维护暂停的示例 |
| CDT 真实用量（最后发送） | 只读查询各账号受监控流量池的实际 CDT 用量，并标注查询时间 |

前十一张是明确标注的示例，不代表真实事件；最后发送的用量汇总来自本次 CDT 查询。长汇总可能拆为多张卡片。两种模式均不启停实例、不修改运行状态，可在服务运行时执行。通知测试需要有效账号凭据、CDT 查询权限和 Webhook。

通知测试成功时终端显示 `Notification test completed: ok`。任何卡片发送或账号用量查询失败时，仍会继续其余测试并尝试发送最后的汇总，然后返回非零退出码；查询失败的账号会明确标记，使用前修正配置错误。确认诊断结果正确且群内收到测试卡片后启动：

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

# 测试全部通知类型，并在最后查询真实 CDT 用量
docker compose run --rm monitor --test-notification

# 修改配置后重新创建容器
docker compose up -d --force-recreate

# 更新程序
git pull --ff-only
docker compose up -d --build

# 停止服务，保留状态数据
docker compose down
```

凭据文件每轮重新读取，原地修改后会在下一轮生效；编辑器替换文件后，应重新创建容器以确保更新挂载。服务不需要开放入站端口。

## 配置说明

`accounts.json` 顶层配置如下：

| 字段 | 说明 |
|---|---|
| `report_hour` | 日报发送小时，使用北京时间，整数 `0`～`23`，省略时默认 `5` |
| `accounts` | 非空账号数组，每个阿里云账号对应一个条目 |

将同一账号全部受监控实例放入该条目的 `instances` 数组。

| 使用场景 | 配置方式 |
|---|---|
| 单账号、单实例 | 数组中保留一个账号，`instances` 中保留一个实例 |
| 单账号、多实例 | 在同一个账号的 `instances` 中增加对象，可配置不同地域 |
| 多账号、多实例 | 在 `accounts` 数组中增加账号对象，每个账号填写自己的凭据、阈值和实例 |

阈值按账号的共享池设置，同一池中的实例共用该阈值。不要将同一账号拆为多个条目，也不要使用多组 AccessKey 重复配置该账号。

账号条目字段：

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

`limits_gb` 及其中任一阈值均可省略。`name` 可以使用自定义别名或账号 UID，仅用于标识账号。未知字段会导致配置被拒绝；未知地域会阻止对应账号的控制。

### 地域对照表

在 ECS 实例详情中查看地域，并将对应 Region ID 填入 `instances[].region`。填写地域 ID（如 `ap-southeast-1`），不要填写可用区 ID（如 `ap-southeast-1a`）。**新加坡是 `ap-southeast-1`，日本东京是 `ap-northeast-1`。**

下表与当前 [监控程序](src/monitor.py) 的 `REGIONS` 列表一致，流量池一栏表示本项目的统计分类。地域开放状态及账号实际可用范围以阿里云控制台为准。

| 地域 | Region ID | 流量池 |
|---|---|---|
| 华北1（青岛） | `cn-qingdao` | 中国内地 |
| 华北2（北京） | `cn-beijing` | 中国内地 |
| 华北3（张家口） | `cn-zhangjiakou` | 中国内地 |
| 华北5（呼和浩特） | `cn-huhehaote` | 中国内地 |
| 华北6（乌兰察布） | `cn-wulanchabu` | 中国内地 |
| 华东1（杭州） | `cn-hangzhou` | 中国内地 |
| 华东2（上海） | `cn-shanghai` | 中国内地 |
| 华东5（南京，本地地域，关停中） | `cn-nanjing` | 中国内地 |
| 华东6（福州，本地地域，关停中） | `cn-fuzhou` | 中国内地 |
| 华中1（武汉，本地地域） | `cn-wuhan-lr` | 中国内地 |
| 华南1（深圳） | `cn-shenzhen` | 中国内地 |
| 华南2（河源） | `cn-heyuan` | 中国内地 |
| 华南3（广州） | `cn-guangzhou` | 中国内地 |
| 西南1（成都） | `cn-chengdu` | 中国内地 |
| 华北2金融云（北京） | `cn-beijing-finance-1` | 中国内地 |
| 华东1金融云（杭州） | `cn-hangzhou-finance` | 中国内地 |
| 华东2金融云（上海） | `cn-shanghai-finance-1` | 中国内地 |
| 华南1金融云（深圳） | `cn-shenzhen-finance-1` | 中国内地 |
| 中国香港 | `cn-hongkong` | 非内地 |
| 新加坡 | `ap-southeast-1` | 非内地 |
| 澳大利亚（悉尼，已关停） | `ap-southeast-2` | 非内地 |
| 马来西亚（吉隆坡） | `ap-southeast-3` | 非内地 |
| 印度尼西亚（雅加达） | `ap-southeast-5` | 非内地 |
| 菲律宾（马尼拉） | `ap-southeast-6` | 非内地 |
| 泰国（曼谷） | `ap-southeast-7` | 非内地 |
| 日本（东京） | `ap-northeast-1` | 非内地 |
| 韩国（首尔） | `ap-northeast-2` | 非内地 |
| 印度（孟买，已关停） | `ap-south-1` | 非内地 |
| 美国（弗吉尼亚） | `us-east-1` | 非内地 |
| 美国（硅谷） | `us-west-1` | 非内地 |
| 英国（伦敦） | `eu-west-1` | 非内地 |
| 德国（法兰克福） | `eu-central-1` | 非内地 |
| 阿联酋（迪拜） | `me-east-1` | 非内地 |
| 沙特（利雅得） | `me-central-1` | 非内地 |

程序保留部分历史地域 ID 的识别能力，不代表这些地域仍可新购或继续运行实例。未列出的地域当前不支持，使用前需核实分类并更新程序列表。地域资料见[阿里云 ECS 地域列表](https://help.aliyun.com/zh/ecs/user-guide/regions-and-zones)、[ECS 服务接入点](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-endpoint)、[地域名称变更公告](https://help.aliyun.com/zh/user-center/product-overview/regional-name-change-announcement)和[金融云地域说明](https://help.aliyun.com/zh/document_detail/57348.html)。

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

配置账号凭据与飞书 Webhook 后，可以测试全部通知类型和真实 CDT 用量：

```sh
python -m src.monitor --test-notification
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
