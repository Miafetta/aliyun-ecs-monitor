"""Account-wide CDT pool monitoring for persistent server deployment."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import signal
import threading
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ACTION_NAMES = {"StartInstance": "\u542f\u52a8", "StopInstance": "\u505c\u6b62"}
GB = 1024 ** 3
CST = timezone(timedelta(hours=8))
LIMITS = {"mainland": Decimal(18), "overseas": Decimal(180)}
POOL_NAMES = {"mainland": "\u4e2d\u56fd\u5185\u5730", "overseas": "\u975e\u5185\u5730"}
REGIONS = set("""cn-qingdao cn-beijing cn-zhangjiakou cn-huhehaote cn-wulanchabu
cn-hangzhou cn-shanghai cn-nanjing cn-fuzhou cn-shenzhen cn-heyuan cn-guangzhou
cn-wuhan-lr cn-chengdu cn-hongkong ap-northeast-1 ap-northeast-2 ap-southeast-1
ap-southeast-2 ap-southeast-3 ap-southeast-5 ap-southeast-6 ap-southeast-7
ap-south-1 us-east-1 us-west-1 eu-west-1 eu-central-1 me-east-1 me-central-1
cn-beijing-finance-1 cn-hangzhou-finance cn-shanghai-finance-1
cn-shenzhen-finance-1""".split())


class MonitorError(Exception):
    pass


def pool(region):
    if not isinstance(region, str) or region not in REGIONS:
        raise MonitorError("Unknown or unsupported region; control withheld")
    return "mainland" if region.startswith("cn-") and region != "cn-hongkong" else "overseas"


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MonitorError("Invalid numeric value")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise MonitorError("Invalid numeric value") from None
    if not result.is_finite() or result < 0:
        raise MonitorError("Invalid numeric value")
    return result


def text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise MonitorError("Invalid " + field)
    return value.strip()


def load_config(raw):
    try:
        accounts = json.loads(raw)
    except (ValueError, TypeError):
        raise MonitorError("ALIYUN_ACCOUNTS_JSON must be valid JSON") from None
    if not isinstance(accounts, list) or not accounts:
        raise MonitorError("Accounts must be a nonempty array")
    names, credentials, instances = set(), set(), set()
    for account in accounts:
        if not isinstance(account, dict) or set(account) - {"name", "access_key_id", "access_key_secret", "limits_gb", "instances"}:
            raise MonitorError("Invalid account fields")
        for field in ("name", "access_key_id", "access_key_secret"):
            account[field] = text(account.get(field), field)
        if account["name"] in names or account["access_key_id"] in credentials:
            raise MonitorError("Duplicate account name or access key")
        names.add(account["name"])
        credentials.add(account["access_key_id"])
        limits = account.get("limits_gb", {})
        if not isinstance(limits, dict) or set(limits) - LIMITS.keys():
            raise MonitorError("Invalid limits_gb fields")
        account["limits_gb"] = {k: number(limits.get(k, v)) for k, v in LIMITS.items()}
        if not isinstance(account.get("instances"), list) or not account["instances"]:
            raise MonitorError("Each account needs instances")
        for instance in account["instances"]:
            if not isinstance(instance, dict) or set(instance) - {"id", "region", "name", "paused"}:
                raise MonitorError("Invalid instance fields")
            for field in ("id", "region"):
                instance[field] = text(instance.get(field), field)
            if not re.fullmatch(r"i-[a-zA-Z0-9]+", instance["id"]):
                raise MonitorError("Invalid instance id")
            if not re.fullmatch(r"[a-z][a-z0-9-]+", instance["region"]):
                raise MonitorError("Invalid instance region")
            instance["name"] = text(instance.get("name", instance["id"]), "instance name")
            if type(instance.get("paused", False)) is not bool:
                raise MonitorError("paused must be a JSON boolean")
            identity = (account["access_key_id"], instance["region"], instance["id"])
            if identity in instances:
                raise MonitorError("Duplicate instance")
            instances.add(identity)
    return accounts


def load_settings(raw):
    try:
        config = json.loads(raw)
    except (ValueError, TypeError):
        raise MonitorError("Account configuration must be valid JSON") from None
    if isinstance(config, list):
        return load_config(raw), 5
    if not isinstance(config, dict) or set(config) - {"report_hour", "accounts"}:
        raise MonitorError("Invalid configuration fields")
    hour = config.get("report_hour", 5)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise MonitorError("report_hour must be an integer from 0 to 23")
    return load_config(json.dumps(config.get("accounts"))), hour


def aggregate(data, required):
    if not isinstance(data, dict) or "Code" in data or data.get("Success") is False:
        raise MonitorError("CDT returned an error")
    details = data.get("TrafficDetails")
    if not isinstance(details, list) or not details:
        raise MonitorError("CDT traffic data is missing or empty")
    totals, seen, covered = {k: Decimal(0) for k in LIMITS}, set(), set()
    for row in details:
        if not isinstance(row, dict):
            raise MonitorError("Invalid CDT detail")
        region = row.get("BusinessRegionId")
        category = pool(region)
        isp = row.get("ISPType", "")
        if not isinstance(isp, str) or (region, isp) in seen:
            raise MonitorError("Duplicate or invalid CDT detail")
        seen.add((region, isp))
        covered.add(category)
        # ProductTrafficDetails and TrafficTierDetails subdivide the same total.
        value = number(row.get("Traffic"))
        if value != value.to_integral_value():
            raise MonitorError("CDT traffic must be whole bytes")
        totals[category] += value
    if not set(required) <= covered:
        raise MonitorError("A monitored traffic pool is missing")
    return totals


class Aliyun:
    def __init__(self, account):
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_ as sdk_ssl
        # Vendored urllib3 checks SNI after importing the removed ssl.wrap_socket.
        sdk_ssl.HAS_SNI = ssl.HAS_SNI
        self.account = account
        self.clients = {}
        self.client_type = AcsClient

    def call(self, service, region, action, **params):
        from aliyunsdkcore.request import CommonRequest
        if region not in self.clients:
            self.clients[region] = self.client_type(self.account["access_key_id"], self.account["access_key_secret"], region, auto_retry=False)
        request = CommonRequest()
        request.set_domain(service + ".aliyuncs.com")
        request.set_version("2021-08-13" if service == "cdt" else "2014-05-26")
        request.set_action_name(action)
        request.set_method("POST")
        request.set_protocol_type("https")
        request.set_accept_format("json")
        request.set_connect_timeout(10)
        request.set_read_timeout(30)
        for key, value in params.items():
            request.add_query_param(key, value)
        try:
            result = json.loads(self.clients[region].do_action_with_exception(request))
        except Exception:
            raise MonitorError("Aliyun " + action + " failed; check RAM permissions and network") from None
        if not isinstance(result, dict) or "Code" in result:
            raise MonitorError("Invalid Aliyun response for " + action)
        return result

    def traffic(self):
        return self.call("cdt", "cn-hangzhou", "ListCdtInternetTraffic")

    def status(self, instance):
        result = self.call("ecs", instance["region"], "DescribeInstances", RegionId=instance["region"], InstanceIds=json.dumps([instance["id"]]))
        rows = result.get("Instances", {}).get("Instance", [])
        if len(rows) != 1 or rows[0].get("InstanceId") != instance["id"] or rows[0].get("Status") not in {"Running", "Stopped", "Starting", "Stopping", "Pending", "Rebooting"}:
            raise MonitorError("Instance is missing or has an unknown state")
        return rows[0]["Status"]

    def act(self, instance, action):
        self.call("ecs", instance["region"], action, RegionId=instance["region"], InstanceId=instance["id"])


def cards(title, sections):
    def card(elements):
        return {"msg_type": "interactive", "card": {"schema": "2.0", "header": {"template": "blue", "title": {"tag": "plain_text", "content": "ECS Monitor \xb7 " + title}}, "body": {"elements": elements}}}
    elements = []
    for section in sections:
        # Bound both UTF-8 bytes and component count, including an oversized section.
        for offset in range(0, len(section), 1500):
            content = section[offset:offset + 1500]
            addition = [{"tag": "hr"}]
            if offset == 0 and content.startswith("### "):
                heading, _, content = content.partition("\n")
                addition.append({"tag": "markdown", "content": heading[4:], "text_size": "heading"})
            if content:
                addition.append({"tag": "markdown", "content": content})
            candidate = card(elements + addition)
            if elements and (len(json.dumps(candidate, ensure_ascii=False).encode()) > 24000 or len(elements) + len(addition) > 100):
                yield card(elements)
                elements = []
            elements.extend(addition)
    if elements:
        yield card(elements)


class Feishu:
    def __init__(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "open.feishu.cn" or not re.fullmatch(r"/open-apis/bot/v2/hook/[a-zA-Z0-9-]+", parsed.path) or parsed.query or parsed.fragment:
            raise MonitorError("FEISHU_WEBHOOK_URL must be a Feishu custom bot HTTPS URL")
        self.url = url

    def send(self, title, sections):
        for body in cards(title, sections):
            request = Request(self.url, data=json.dumps(body, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=20) as response:
                    if response.status != 200:
                        raise MonitorError("Feishu HTTP failure")
                    result = json.load(response)
                if type(result.get("code")) is not int or result["code"] != 0:
                    raise MonitorError("Feishu rejected message; check bot keyword and security settings")
            except Exception:
                raise MonitorError("Feishu delivery failed; check bot keyword and network") from None


def state_key(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def load_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") == 1 and isinstance(value.get("events"), dict) and isinstance(value.get("pending"), dict):
            events = {k: v for k, v in value["events"].items() if re.fullmatch(r"[a-f0-9]{32}", k) and type(v) in (int, float) and 0 <= v <= time.time()}
            pending = {k: {f: v[f] for f in ("action", "at", "notified")} for k, v in value["pending"].items() if re.fullmatch(r"[a-f0-9]{32}", k) and isinstance(v, dict) and v.get("action") in ("StartInstance", "StopInstance") and type(v.get("at")) in (int, float) and 0 <= v["at"] <= time.time() and type(v.get("notified")) is bool}
            day = value.get("report_day", "")
            return {"version": 1, "events": events, "pending": pending, "report_day": day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""}
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        pass
    return {"version": 1, "events": {}, "pending": {}, "report_day": ""}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class Monitor:
    def __init__(self, state, notifier, diagnostic=False, now=None):
        self.state = state
        self.notifier = notifier
        self.diagnostic = diagnostic
        self.now = now or datetime.now(CST)
        self.failed = False
        self.sections = []

    def notify(self, key, title, content, cooldown=3600):
        if self.diagnostic:
            return True
        stamp = self.now.timestamp()
        if stamp - self.state["events"].get(key, 0) < cooldown:
            return True
        try:
            self.notifier.send(title, [content])
        except MonitorError:
            self.failed = True
            print("ERROR: Feishu delivery failed", file=sys.stderr)
            return False
        self.state["events"][key] = stamp
        return True

    def account(self, account, api):
        name = account["name"]
        try:
            totals = aggregate(api.traffic(), {pool(i["region"]) for i in account["instances"]})
            statuses = {i["id"]: api.status(i) for i in account["instances"] if not i.get("paused")}
        except Exception as error:
            self.failed = True
            self.sections.append(f"### \u26a0\ufe0f {name}\n\u67e5\u8be2\u5f02\u5e38\uff1b\u672c\u8f6e\u4fdd\u6301\u8be5\u8d26\u53f7\u5168\u90e8\u5b9e\u4f8b\u72b6\u6001\u3002")
            self.notify(state_key(name, "query"), "\u67e5\u8be2\u5f02\u5e38", f"### \u26a0\ufe0f {name}\nCDT \u6216 ECS \u67e5\u8be2\u5f02\u5e38\uff1b\u672c\u8f6e\u672a\u6267\u884c\u63a7\u5236\u3002\u8bf7\u8fd0\u884c\u53ea\u8bfb\u8bca\u65ad\u5e76\u68c0\u67e5\u6743\u9650\u3001\u5730\u57df\u548c\u54cd\u5e94\u5b8c\u6574\u6027\u3002")
            print("ERROR: Account query failed; control withheld", file=sys.stderr)
            if isinstance(error, MonitorError):
                print("ERROR: " + str(error), file=sys.stderr)
            return
        for category in sorted({pool(i["region"]) for i in account["instances"]}):
            self.sections.append(f"### \U0001f4ca {name} \xb7 {POOL_NAMES[category]}\n\u5171\u4eab\u6c60\uff1a{totals[category] / GB:.3f} GB / {account['limits_gb'][category]} GB")
        for instance in account["instances"]:
            category = pool(instance["region"])
            label = f"{name} \xb7 {POOL_NAMES[category]} \xb7 {instance['name']} ({instance['id']}, {instance['region']})"
            if instance.get("paused"):
                self.sections.append(f"\u23f8\ufe0f {label}\uff1a\u81ea\u52a8\u63a7\u5236\u5df2\u6682\u505c")
                continue
            status = statuses[instance["id"]]
            self.sections.append(f"\U0001f5a5\ufe0f {label}\uff1a{status}")
            if self.diagnostic:
                continue
            key = state_key(name, instance["region"], instance["id"])
            pending = self.state["pending"].get(key)
            if pending:
                action = pending["action"]
                if not pending["notified"]:
                    pending["notified"] = self.notify(state_key(key, "submitted", pending["at"]), "\u8bf7\u6c42\u5df2\u63d0\u4ea4", f"### \U0001f4e8 {label}\n{ACTION_NAMES[action]} \u8bf7\u6c42\u5df2\u63d0\u4ea4\uff1b\u7b49\u5f85\u5b9e\u9645\u72b6\u6001\u786e\u8ba4\u3002", 0)
                target = "Running" if action == "StartInstance" else "Stopped"
                if status == target:
                    if pending["notified"] and self.notify(state_key(key, "completed", pending["at"]), "\u64cd\u4f5c\u5df2\u5b8c\u6210", f"### \u2705 {label}\n{ACTION_NAMES[action]} \u5df2\u5b8c\u6210\uff0c\u67e5\u8be2\u786e\u8ba4\u72b6\u6001\uff1a{target}\u3002", 0):
                        del self.state["pending"][key]
                elif self.now.timestamp() - pending["at"] > 900:
                    self.failed = True
                    self.notify(state_key(key, "timeout", pending["at"]), "\u64cd\u4f5c\u7b49\u5f85\u8d85\u65f6", f"### \u26a0\ufe0f {label}\n{ACTION_NAMES[action]} \u8d85\u8fc7 15 \u5206\u949f\u672a\u786e\u8ba4\uff0c\u5f53\u524d\u72b6\u6001\uff1a{status}\uff1b\u540e\u7eed\u91cd\u65b0\u68c0\u67e5\u3002", 0)
                    del self.state["pending"][key]
                if status != target:
                    continue
            over = totals[category] >= account["limits_gb"][category] * GB
            action = "StopInstance" if over and status == "Running" else "StartInstance" if not over and status == "Stopped" else None
            if over:
                self.notify(state_key(key, "over", self.now.strftime("%Y-%m")), "\u5171\u4eab\u6d41\u91cf\u6c60\u8fbe\u5230\u9608\u503c", f"### \U0001f6d1 {label}\n\u5171\u4eab\u6c60 {totals[category] / GB:.3f} GB\uff0c\u9608\u503c {account['limits_gb'][category]} GB\uff1b\u5f53\u524d {status}\u3002", 86400)
            if action:
                try:
                    api.act(instance, action)
                except Exception:
                    self.failed = True
                    self.notify(state_key(key, action, "failed"), "\u63a7\u5236\u8bf7\u6c42\u5931\u8d25", f"### \u26a0\ufe0f {label}\n{ACTION_NAMES[action]} \u8bf7\u6c42\u5931\u8d25\uff1b\u672a\u786e\u8ba4\u72b6\u6001\u53d8\u5316\uff0c\u4e0b\u8f6e\u91cd\u8bd5\u3002")
                    continue
                # shortcut: keep the latest action; add an outbox if audit delivery is required.
                pending = {"action": action, "at": self.now.timestamp(), "notified": False}
                self.state["pending"][key] = pending
                pending["notified"] = self.notify(state_key(key, "submitted", pending["at"]), "\u8bf7\u6c42\u5df2\u63d0\u4ea4", f"### \U0001f4e8 {label}\n{ACTION_NAMES[action]} \u8bf7\u6c42\u5df2\u63d0\u4ea4\uff1b\u7b49\u5f85\u4e0b\u4e00\u8f6e\u67e5\u8be2\u786e\u8ba4\u5b9e\u9645\u5b8c\u6210\u3002", 0)

    def report(self, hour):
        day = self.now.strftime("%Y-%m-%d")
        if self.diagnostic or self.failed or self.now.hour < hour or self.state["report_day"] == day:
            return
        try:
            self.notifier.send("\u6bcf\u65e5\u62a5\u544a \xb7 " + day, self.sections)
        except MonitorError:
            self.failed = True
            print("ERROR: Daily report delivery failed", file=sys.stderr)
        else:
            self.state["report_day"] = day


def setting(name, file_name):
    path = os.environ.get(file_name)
    return Path(path).read_text(encoding="utf-8-sig").strip() if path else os.environ.get(name, "")


def run_once(diagnostic=False):
    path = Path(".state/monitor.json")
    try:
        accounts, hour = load_settings(setting("ALIYUN_ACCOUNTS_JSON", "ALIYUN_ACCOUNTS_FILE"))
        notifier = None if diagnostic else Feishu(setting("FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_FILE"))
        state = load_state(path)
        monitor = Monitor(state, notifier, diagnostic)
        for account in accounts:
            monitor.account(account, Aliyun(account))
        if diagnostic:
            for section in monitor.sections:
                print(section)
        else:
            monitor.report(hour)
            cutoff = monitor.now.timestamp() - 32 * 86400
            state["events"] = {k: v for k, v in state["events"].items() if v > cutoff}
            save_state(path, state)
        return int(monitor.failed)
    except MonitorError as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    except Exception:
        # SDK exceptions may contain signed URLs or credentials; never print them.
        print("ERROR: Monitor failed; check configuration, dependencies and network. See docs/troubleshooting.md", file=sys.stderr)
        return 1


def test_notifications():
    try:
        accounts, _ = load_settings(setting("ALIYUN_ACCOUNTS_JSON", "ALIYUN_ACCOUNTS_FILE"))
        notifier = Feishu(setting("FEISHU_WEBHOOK_URL", "FEISHU_WEBHOOK_FILE"))
    except Exception:
        print("ERROR: Notification test configuration invalid; check accounts and Webhook files", file=sys.stderr)
        return 1
    failed = False

    def send(title, sections):
        nonlocal failed
        try:
            notifier.send("\u901a\u77e5\u6d4b\u8bd5 \xb7 " + title, sections)
        except Exception:
            failed = True
            print("ERROR: Test card delivery failed", file=sys.stderr)

    sample = "\u6d4b\u8bd5\u793a\u4f8b\uff0c\u975e\u771f\u5b9e\u4e8b\u4ef6\uff1b\u672a\u6267\u884c\u5b9e\u4f8b\u542f\u505c\u3002"
    send("\u67e5\u8be2\u5f02\u5e38", [f"### \u26a0\ufe0f \u793a\u4f8b\u8d26\u53f7\n{sample}\nCDT \u6216\u5b9e\u4f8b\u72b6\u6001\u67e5\u8be2\u5931\u8d25\uff0c\u672c\u8f6e\u8df3\u8fc7\u8be5\u8d26\u53f7\u63a7\u5236\u3002"])
    send("\u5171\u4eab\u6d41\u91cf\u6c60\u8fbe\u5230\u9608\u503c", [f"### \U0001f6d1 \u793a\u4f8b\u8d26\u53f7 \xb7 \u975e\u5185\u5730\n{sample}\n\u5171\u4eab\u6c60 180.000 GB\uff0c\u9608\u503c 180 GB\uff1b\u5b9e\u4f8b\u72b6\u6001 Running\u3002"])
    for action in ACTION_NAMES.values():
        for title, icon, description in (
            ("\u8bf7\u6c42\u5df2\u63d0\u4ea4", "\U0001f4e8", "\u8bf7\u6c42\u5df2\u63a5\u53d7\uff0c\u7b49\u5f85\u5b9e\u4f8b\u72b6\u6001\u786e\u8ba4"),
            ("\u64cd\u4f5c\u5df2\u5b8c\u6210", "\u2705", "\u5df2\u67e5\u8be2\u786e\u8ba4\u76ee\u6807\u72b6\u6001"),
            ("\u64cd\u4f5c\u7b49\u5f85\u8d85\u65f6", "\u26a0\ufe0f", "\u8d85\u8fc7 15 \u5206\u949f\u672a\u786e\u8ba4\u76ee\u6807\u72b6\u6001"),
            ("\u63a7\u5236\u8bf7\u6c42\u5931\u8d25", "\u26a0\ufe0f", "\u8bf7\u6c42\u5931\u8d25\uff0c\u540e\u7eed\u8f6e\u6b21\u91cd\u8bd5"),
        ):
            send(title + " \xb7 " + action, [f"### {icon} \u793a\u4f8b\u5b9e\u4f8b\n{sample}\n{action}\uff1a{description}\u3002"])
    send("\u6bcf\u65e5\u62a5\u544a", [f"### \U0001f4ca \u793a\u4f8b\u8d26\u53f7 \xb7 \u975e\u5185\u5730\n{sample}\n\u5171\u4eab\u6c60\uff1a12.500 GB / 180 GB", "### \U0001f5a5\ufe0f \u793a\u4f8b\u5b9e\u4f8b\n\u72b6\u6001\uff1aRunning", "### \u23f8\ufe0f \u793a\u4f8b\u7ef4\u62a4\u5b9e\u4f8b\n\u81ea\u52a8\u63a7\u5236\u5df2\u6682\u505c"])
    sections = ["### CDT \u771f\u5b9e\u7528\u91cf\n\u67e5\u8be2\u65f6\u95f4\uff08\u5317\u4eac\u65f6\u95f4\uff09\uff1a" + datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + "\n\u6570\u636e\u6765\u81ea CDT \u672c\u6b21\u8fd4\u56de\u503c\uff0c\u7528\u91cf\u66f4\u65b0\u53ef\u80fd\u5b58\u5728\u5ef6\u8fdf\u3002"]
    for account in accounts:
        try:
            required = {pool(i["region"]) for i in account["instances"]}
            totals = aggregate(Aliyun(account).traffic(), required)
        except Exception:
            failed = True
            sections.append(f"### \u26a0\ufe0f {account['name']}\nCDT \u67e5\u8be2\u5931\u8d25\uff0c\u672a\u83b7\u53d6\u7528\u91cf\u3002\u8bf7\u68c0\u67e5\u8d26\u53f7\u6743\u9650\u3001\u5730\u57df\u4e0e\u7f51\u7edc\u3002")
            print("ERROR: CDT test query failed", file=sys.stderr)
            continue
        for category in sorted(required):
            sections.append(f"### \U0001f4ca {account['name']} \xb7 {POOL_NAMES[category]}\n\u5171\u4eab\u6c60\uff1a{totals[category] / GB:.3f} GB / {account['limits_gb'][category]} GB\n\u539f\u59cb\u7528\u91cf\uff1a{totals[category]} \u5b57\u8282")
    send("CDT \u771f\u5b9e\u7528\u91cf", sections)
    print("Notification test completed: " + ("failed" if failed else "ok"), flush=True)
    return int(failed)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--diagnose", action="store_true", help="Query only; no control, notifications or state writes")
    mode.add_argument("--preview", action="store_true", help="Print example cards without credentials or network")
    mode.add_argument("--serve", action="store_true", help="Run continuously, checking every five minutes")
    mode.add_argument("--test-notification", action="store_true", help="Send all sample notifications, then query CDT; no control or state writes")
    args = parser.parse_args()
    if args.test_notification:
        return test_notifications()
    if args.preview:
        print(json.dumps(list(cards("\u6bcf\u65e5\u62a5\u544a", ["### \U0001f4ca \u793a\u4f8b\u8d26\u53f7 \xb7 \u975e\u5185\u5730\n\u5171\u4eab\u6c60\uff1a12.500 GB / 180 GB", "### \U0001f5a5\ufe0f \u793a\u4f8b\u5b9e\u4f8b\n\u72b6\u6001\uff1aRunning\n\u2705 \u7528\u91cf\u4f4e\u4e8e\u9608\u503c"])), ensure_ascii=False, indent=2))
        return 0
    if args.diagnose:
        return run_once(diagnostic=True)
    path = Path(".state")
    path.mkdir(parents=True, exist_ok=True)
    with (path / "monitor.lock").open("a") as lock:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("ERROR: Another monitor is using this state directory", file=sys.stderr)
                return 1
        if not args.serve:
            return run_once()
        return serve()


def serve():
    stop = threading.Event()
    def shutdown(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("Monitor started; check interval: 300 seconds", flush=True)
    while not stop.is_set():
        started = time.monotonic()
        result = run_once()
        print(f"Check completed: {'failed' if result else 'ok'}", flush=True)
        stop.wait(max(1, 300 - (time.monotonic() - started)))
    print("Monitor stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
