"""探针 A：录制 N 天真实外生信号流。

回答一个问题：这个世界够不够有心跳？
如果一天几十条里没有一条值得理会，说明 World 选错了，整个实验白搭。

产物 fixtures/signals.json 同时作为阶段 0 的回放 fixture（docs/03 §6）。
不静默跳过失败的源 —— 失败会显式列在报告里。
"""
import hashlib, json, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

UA = {"User-Agent": "driving-agent-probe/0.1 (research)"}
failures: list[str] = []


def fetch(url: str, timeout: int = 60) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def sig(source, channel, ts, title, summary, url):
    body = (summary or "").strip().replace("\n", " ")
    return {
        "id": hashlib.sha1(f"{source}|{url}|{title}".encode()).hexdigest()[:12],
        "source": source, "channel": channel,
        "day": ts.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "title": (title or "").strip().replace("\n", " "),
        "summary": body[:600],
        "url": url,
    }


def from_arxiv(cfg, since):
    """分页拉取直到越过时间窗。

    不分页会被 max_results 截断，导致窗口早几天的信号量虚低 —— fixture 失真。
    """
    out, page = [], cfg.get("page_size", 200)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for cat in cfg["categories"]:
        n, start = 0, 0
        try:
            while start < cfg["max_per_category"]:
                q = urllib.parse.urlencode({
                    "search_query": f"cat:{cat}", "sortBy": "submittedDate",
                    "sortOrder": "descending", "start": start, "max_results": page})
                entries = ET.fromstring(
                    fetch(f"http://export.arxiv.org/api/query?{q}")).findall("a:entry", ns)
                if not entries:
                    break
                oldest_in_window = False
                for e in entries:
                    ts = datetime.fromisoformat(
                        e.find("a:published", ns).text.replace("Z", "+00:00"))
                    if ts < since:
                        oldest_in_window = True
                        continue
                    out.append(sig("arxiv", cat, ts, e.find("a:title", ns).text,
                                   e.find("a:summary", ns).text, e.find("a:id", ns).text))
                    n += 1
                start += page
                time.sleep(5)          # arXiv 要求礼貌间隔（30 天窗口分页更深）
                if oldest_in_window:   # 本页已出现窗口外的条目，说明拉够了
                    break
            print(f"  arxiv/{cat}: {n} 条")
        except Exception as ex:
            failures.append(f"arxiv/{cat}: {type(ex).__name__}: {ex}")
    return out


def from_github(cfg, since):
    out = []
    day = since.strftime("%Y-%m-%d")
    for query in cfg["queries"]:
        try:
            q = urllib.parse.urlencode({
                "q": f"{query} pushed:>={day}", "sort": "updated", "per_page": 30})
            data = json.loads(fetch(f"https://api.github.com/search/repositories?{q}"))
            if "items" not in data:
                failures.append(f"github/{query}: {data.get('message', data)}")
                continue
            n = 0
            for r in data["items"]:
                ts = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
                if ts < since:
                    continue
                out.append(sig("github", query, ts,
                               f'{r["full_name"]} ({r["stargazers_count"]}★)',
                               r.get("description") or "", r["html_url"]))
                n += 1
            print(f"  github/{query!r}: {n} 条")
            time.sleep(7)  # 未认证 search 限流 10/min
        except Exception as ex:
            failures.append(f"github/{query}: {type(ex).__name__}: {ex}")
    return out


def from_rss(cfg, since):
    out = []
    for feed in cfg["feeds"]:
        try:
            root = ET.fromstring(fetch(feed["url"]))
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            n = 0
            for it in items:
                def pick(*tags):
                    for t in tags:
                        el = it.find(t)
                        if el is not None and (el.text or el.get("href")):
                            return el.text or el.get("href")
                    return ""
                raw = pick("pubDate", "{http://www.w3.org/2005/Atom}published",
                           "{http://www.w3.org/2005/Atom}updated")
                ts = None
                for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
                    try:
                        ts = datetime.strptime(raw.strip(), fmt); break
                    except (ValueError, AttributeError):
                        pass
                if ts is None:
                    try:
                        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
                out.append(sig("rss", feed["name"], ts,
                               pick("title", "{http://www.w3.org/2005/Atom}title"),
                               pick("description", "{http://www.w3.org/2005/Atom}summary"),
                               pick("link", "{http://www.w3.org/2005/Atom}link")))
                n += 1
            print(f"  rss/{feed['name']}: {n} 条")
        except Exception as ex:
            failures.append(f"rss/{feed['name']}: {type(ex).__name__}: {ex}")
    return out


def main():
    cfg = yaml.safe_load(Path("probe/sources.yaml").read_text(encoding="utf-8"))
    days = int(sys.argv[1]) if len(sys.argv) > 1 else cfg["window_days"]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    print(f"录制窗口：最近 {days} 天（自 {since:%Y-%m-%d %H:%M} UTC）\n")

    signals = []
    for name, fn in (("arxiv", from_arxiv), ("github", from_github), ("rss", from_rss)):
        if cfg["sources"][name]["enabled"]:
            print(f"[{name}]")
            signals += fn(cfg["sources"][name], since)

    seen, uniq = set(), []
    for s in signals:
        if s["id"] not in seen:
            seen.add(s["id"]); uniq.append(s)
    uniq.sort(key=lambda s: s["ts"])

    out = Path("fixtures/signals.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"recorded_at": datetime.now(timezone.utc).isoformat(), "window_days": days,
         "concern": cfg["identity"]["concern"], "failures": failures, "signals": uniq},
        ensure_ascii=False, indent=2), encoding="utf-8")

    per_day = Counter(s["day"] for s in uniq)
    print(f"\n{'='*52}\n共 {len(uniq)} 条（去重后），落盘 {out}\n")
    print("按天分布：")
    for d in sorted(per_day):
        print(f"  {d}  {per_day[d]:4d} 条  {'█' * min(per_day[d] // 2, 60)}")
    print(f"\n日均 {len(uniq)/max(len(per_day),1):.1f} 条")
    print("\n按来源：")
    for k, v in Counter(f'{s["source"]}/{s["channel"]}' for s in uniq).most_common():
        print(f"  {k:34s} {v:4d}")
    if failures:
        print(f"\n⚠️  失败的源 {len(failures)} 个（不静默跳过）：")
        for f in failures:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
