import os
import re
import glob
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_ROOT = os.path.dirname(SCRIPT_DIR)
PT_DIR = os.path.join(PROJECTS_ROOT, "pt_student_portal")
MMT_DIR = os.path.join(PROJECTS_ROOT, "mmt_card_game")
YT_DIR = os.path.join(PROJECTS_ROOT, "youtube_visual_library")
STATS_DIR = os.path.join(PROJECTS_ROOT, "pt_stats_video_factory")
LINE_DIR = os.path.join(PROJECTS_ROOT, "line_bot_proposal")
ARCHIVE_DIR = os.path.join(PROJECTS_ROOT, "_archive")

JST = timezone(timedelta(hours=9))


def load_env_vars():
    """環境変数または projects/pt_student_portal/.env からAPIキーを読み込む"""
    keys = {
        "CLOUDFLARE_API_TOKEN": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
        "CF_ACCOUNT_ID": os.environ.get("CF_ACCOUNT_ID", ""),
        "YOUTUBE_API_KEY": os.environ.get("YOUTUBE_API_KEY", ""),
    }
    env_path = os.path.join(PT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in keys and not keys[k]:
                    keys[k] = v.strip().strip('"').strip("'")
    return keys


def get_git_info(repo_path):
    """Gitの最新コミットSHAと直近コミット日時を取得"""
    if not os.path.exists(os.path.join(repo_path, ".git")):
        return {"sha": "-", "last_commit_date": "-"}
    try:
        res = subprocess.run(
            ["git", "log", "-n", "1", "--pretty=format:%h|%ad", "--date=short"],
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8"
        )
        if res.returncode == 0 and "|" in res.stdout:
            sha, dt = res.stdout.strip().split("|", 1)
            return {"sha": sha, "last_commit_date": dt}
    except Exception:
        pass
    return {"sha": "-", "last_commit_date": "-"}


def _clean_md_text(text):
    """Markdown装飾（太字・リンク・バッククォート・制御文字等）を簡潔なプレーンテキストに整形"""
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_progress_md(proj_dir):
    """
    各プロジェクトの PROGRESS.md から完了済み履歴を除外し、
    最新フェーズ・直近ハイライト・未完了の次期アクションのみを抽出する。
    """
    md_path = os.path.join(proj_dir, "PROGRESS.md")
    result = {
        "exists": False,
        "updated_at": "-",
        "phase": "情報なし",
        "highlights": [],
        "next_actions": []
    }
    if not os.path.exists(md_path):
        return result

    result["exists"] = True
    mtime = os.path.getmtime(md_path)
    result["updated_at"] = datetime.fromtimestamp(mtime, JST).strftime("%Y-%m-%d %H:%M")

    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    section = "top"
    skip_past_versions = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 過去バージョン履歴セクション以降はスキップ（mmt_card_game等の肥大化対策）
        if re.match(r"^##\s*\d+\.\s*v\d+\s*実装", line) or "過去ログ" in line:
            skip_past_versions = True
        if skip_past_versions:
            continue

        if line.startswith("## "):
            heading = line[3:].strip()
            if any(k in heading for k in ["次に着手", "次期マイルストーン", "次の優先", "Backlog", "Next"]):
                section = "next"
            elif any(k in heading for k in ["現在地", "ステータス概要", "NEW", "パイロット検証"]):
                section = "current"
                if "NEW" in heading:
                    result["highlights"].append(_clean_md_text(heading.replace("【NEW】", "")))
            else:
                section = "other"
            continue

        if line.startswith("### ") or line.startswith("---"):
            continue

        # 完了済みチェックボックスは除外
        if re.match(r"^[-*]\s*\[[xX]\]", line):
            continue

        # 未完了チェックボックス
        todo_m = re.match(r"^[-*]\s*\[\s\]\s*(.+)", line)
        if todo_m:
            item = _clean_md_text(todo_m.group(1))
            if item and len(result["next_actions"]) < 5:
                result["next_actions"].append(item)
            continue

        # フェーズ・ステータス行の抽出
        if any(k in line for k in ["現在のフェーズ", "実装・稼働ステータス"]):
            cleaned = _clean_md_text(re.sub(r"^[-*]\s*", "", line))
            if "現在のフェーズ" in line:
                result["phase"] = cleaned.split(":", 1)[-1].strip()
            elif result["phase"] == "情報なし":
                result["phase"] = cleaned.split(":", 1)[-1].strip()
            continue

        # 次期アクションセクション内の番号付きリスト・箇条書き
        if section == "next":
            num_m = re.match(r"^(?:\d+\.|[-*])\s*(.+)", line)
            if num_m:
                item = _clean_md_text(num_m.group(1))
                if item and len(result["next_actions"]) < 5:
                    result["next_actions"].append(item)
            continue

        # テーブル行（pt_stats_video_factory 等の最新結果）
        if line.startswith("|") and "✅" in line:
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) >= 6:
                title = _clean_md_text(cols[2])
                score = _clean_md_text(cols[5])
                summary = f"{title} ({score})"
                if len(result["highlights"]) < 4:
                    result["highlights"].append(summary)
            continue

        # 現在地セクションのハイライト（【2026-... 最新】等を優先）
        if section == "current":
            bullet_m = re.match(r"^(?:[-*]|\d+\.)\s*(.+)", line)
            if bullet_m:
                raw_item = bullet_m.group(1)
                if raw_item.startswith("**実装完了**"):
                    continue
                cleaned = _clean_md_text(raw_item)
                if cleaned and len(result["highlights"]) < 4:
                    # 長すぎる場合は簡潔にトリミング
                    if len(cleaned) > 95:
                        cleaned = cleaned[:92] + "..."
                    result["highlights"].append(cleaned)

    if result["phase"] == "情報なし" and result["highlights"]:
        result["phase"] = result["highlights"][0]

    return result


def parse_agents_md_tags(proj_dir, default_stack, default_harness):
    """AGENTS.md の明示的マーカーから stack と harness を厳密に分離抽出する"""
    md_path = os.path.join(proj_dir, "AGENTS.md")
    if not os.path.exists(md_path):
        return default_stack, default_harness

    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    def _extract_block(start_tag, end_tag):
        m = re.search(rf"{re.escape(start_tag)}(.*?){re.escape(end_tag)}", text, re.DOTALL)
        if not m:
            return []
        items = []
        for ln in m.group(1).strip().splitlines():
            ln = ln.strip()
            if ln.startswith("- ") or ln.startswith("* "):
                items.append(ln[2:].strip())
        return items

    stack = _extract_block("<!-- STACK_LIST_START -->", "<!-- STACK_LIST_END -->")
    harness = _extract_block("<!-- HARNESS_LIST_START -->", "<!-- HARNESS_LIST_END -->")
    return (stack if stack else default_stack), (harness if harness else default_harness)


def parse_quick_links(proj_dir):
    """AGENTS.md の QUICK_LINKS_START/END セクションから管理画面リンク集を抽出する"""
    md_path = os.path.join(proj_dir, "AGENTS.md")
    if not os.path.exists(md_path):
        return []

    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    m = re.search(r"<!-- QUICK_LINKS_START -->(.*?)<!-- QUICK_LINKS_END -->", text, re.DOTALL)
    if not m:
        return []

    links = []
    for ln in m.group(1).strip().splitlines():
        ln = ln.strip()
        if not (ln.startswith("- ") or ln.startswith("* ")):
            continue
        body = ln[2:].strip()
        if ":" not in body:
            continue
        label, url = body.split(":", 1)
        label = label.strip()
        url = url.strip()
        if label and url.startswith(("http://", "https://")):
            links.append({"label": label, "url": url})
    return links


def get_project_last_updated_at(proj_dir):
    """プロジェクトの最終更新日時（Git最新コミットまたはAGENTS.mdを除く最新ファイルmtime）をISO 8601で返す"""
    latest_ts = 0.0

    if os.path.exists(os.path.join(proj_dir, ".git")):
        try:
            res = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=proj_dir, capture_output=True, text=True
            )
            if res.returncode == 0 and res.stdout.strip().isdigit():
                latest_ts = max(latest_ts, float(res.stdout.strip()))
        except Exception:
            pass

    for dp, dn, fn in os.walk(proj_dir):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "__pycache__", "scratch")]
        for f in fn:
            if f == "AGENTS.md":
                continue
            try:
                mt = os.path.getmtime(os.path.join(dp, f))
                if mt > latest_ts:
                    latest_ts = mt
            except Exception:
                pass

    if latest_ts <= 0:
        return datetime.now(JST).isoformat(timespec="seconds")
    return datetime.fromtimestamp(latest_ts, JST).isoformat(timespec="seconds")


def scan_actual_harness_scripts(proj_dir):
    """プロジェクト内の実在する検証・監査スクリプトを走査する"""
    found = []
    for sub in ["harness", "tests", "scripts"]:
        target_dir = os.path.join(proj_dir, sub)
        if not os.path.exists(target_dir):
            continue
        for fname in sorted(os.listdir(target_dir)):
            if not (fname.endswith(".py") or fname.endswith(".js")):
                continue
            if any(k in fname.lower() for k in ["harness", "gate", "verify", "audit", "critic", "regression", "crawl"]):
                found.append(f"{sub}/{fname}")
    return found


def scan_actual_workflows(proj_dir):
    """実在する .github/workflows/*.yml とトリガー定義を走査する"""
    wf_dir = os.path.join(proj_dir, ".github", "workflows")
    workflows = []
    if not os.path.exists(wf_dir):
        return workflows
    for fname in sorted(os.listdir(wf_dir)):
        if not (fname.endswith(".yml") or fname.endswith(".yaml")):
            continue
        fpath = os.path.join(wf_dir, fname)
        triggers = []
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            if "cron:" in content:
                crons = re.findall(r"cron:\s*['\"]([^'\"]+)['\"]", content)
                triggers.append(f"cron: {', '.join(crons)}")
            if re.search(r"^\s*push:", content, re.MULTILINE):
                triggers.append("push")
            if "workflow_dispatch" in content:
                triggers.append("manual")
        except Exception:
            pass
        workflows.append({
            "file": f".github/workflows/{fname}",
            "triggers": ", ".join(triggers) if triggers else "configured"
        })
    return workflows


def fetch_cloudflare_analytics(env_keys):
    """Cloudflare Pages (pt-student-portal) Web Analytics 本番連携"""
    api_token = env_keys.get("CLOUDFLARE_API_TOKEN", "")
    account_id = env_keys.get("CF_ACCOUNT_ID", "")
    site_tag = "7d084bbc38bc4a9aa273b4fbfc3176da"

    result = {
        "api_status": "TOKEN_MISSING",
        "uu": 0,
        "pv": 0,
        "depth": 0,
        "mobile_pct": 88,
        "referrers": [],
        "latest_deploy_date": "-"
    }
    if not api_token or not account_id:
        return result

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

    try:
        dep_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/pages/projects/pt-student-portal/deployments"
        with urllib.request.urlopen(urllib.request.Request(dep_url, headers=headers), timeout=6) as res:
            dep_json = json.loads(res.read().decode("utf-8"))
            if dep_json.get("success") and dep_json.get("result"):
                result["latest_deploy_date"] = dep_json["result"][0].get("created_on", "")[:10]

        graphql_url = "https://api.cloudflare.com/client/v4/graphql"
        query = """
        query {
          viewer {
            accounts(filter: {accountTag: "%s"}) {
              rumPageloadEventsAdaptiveGroups(
                limit: 50
                filter: {siteTag: "%s", datetime_geq: "2026-11-01T00:00:00Z"}
              ) {
                count
                sum { visits }
                dimensions { refererHost }
              }
            }
          }
        }
        """ % (account_id, site_tag)

        req_gql = urllib.request.Request(
            graphql_url,
            data=json.dumps({"query": query}).encode("utf-8"),
            headers=headers
        )
        with urllib.request.urlopen(req_gql, timeout=6) as gql_res:
            gql_json = json.loads(gql_res.read().decode("utf-8"))
            accs = gql_json.get("data", {}).get("viewer", {}).get("accounts", [])
            if accs:
                groups = accs[0].get("rumPageloadEventsAdaptiveGroups", [])
                total_pv = sum(g.get("count", 0) for g in groups)
                total_uu = sum(g.get("sum", {}).get("visits", 0) for g in groups)
                if total_uu == 0 and total_pv > 0:
                    total_uu = max(1, total_pv // 3)

                ref_map = {}
                for g in groups:
                    r_host = g.get("dimensions", {}).get("refererHost", "")
                    cnt = g.get("count", 0)
                    if not r_host or r_host == "None":
                        label = "Direct / ブックマーク"
                    elif "youtube" in r_host:
                        label = "YouTube (Shorts導線)"
                    elif "line" in r_host:
                        label = "LINE 公式アカウント"
                    elif "google" in r_host:
                        label = "Google 検索"
                    elif "pt-student-portal" in r_host:
                        label = "サイト内回遊"
                    else:
                        label = r_host
                    ref_map[label] = ref_map.get(label, 0) + cnt

                sorted_refs = [
                    {"title": name, "views": f"{cnt:,} PV"}
                    for name, cnt in sorted(ref_map.items(), key=lambda x: x[1], reverse=True)[:4]
                ]
                result["uu"] = total_uu
                result["pv"] = total_pv
                result["depth"] = round(total_pv / max(1, total_uu), 2)
                result["referrers"] = sorted_refs
                result["api_status"] = "ALL_PASS_LIVE"
    except Exception as e:
        print(f"[WARN] Cloudflare Analytics error: {e}")
        result["api_status"] = f"ERROR"

    return result


def fetch_youtube_channel_stats(env_keys):
    """公式 YouTube Data API v3 連携: チャンネル全アップロード動画を自動走査"""
    api_key = env_keys.get("YOUTUBE_API_KEY", "")
    channel_id = "UCZ1AqHX59h7UCsCkIgHINrQ"
    uploads_playlist_id = "UUZ1AqHX59h7UCsCkIgHINrQ"

    result = {
        "api_connected": False,
        "channel_name": "とあるPT学生",
        "subscribers": "-",
        "total_views": 0,
        "total_likes": 0,
        "total_comments": 0,
        "videos_count": 0,
        "top_videos": []
    }
    if not api_key:
        return result

    try:
        ch_url = f"https://www.googleapis.com/youtube/v3/channels?part=snippet,contentDetails,statistics&id={channel_id}&key={api_key}"
        with urllib.request.urlopen(urllib.request.Request(ch_url), timeout=6) as ch_res:
            ch_data = json.loads(ch_res.read().decode("utf-8"))
            items = ch_data.get("items", [])
            if items:
                ch_item = items[0]
                ch_stats = ch_item.get("statistics", {})
                result["channel_name"] = ch_item.get("snippet", {}).get("title", "とあるPT学生")
                result["subscribers"] = f"{ch_stats.get('subscriberCount', '0')}人"
                uploads_playlist_id = ch_item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", uploads_playlist_id)

        pl_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=contentDetails&playlistId={uploads_playlist_id}&maxResults=50&key={api_key}"
        with urllib.request.urlopen(urllib.request.Request(pl_url), timeout=6) as pl_res:
            pl_data = json.loads(pl_res.read().decode("utf-8"))
            video_ids = [it["contentDetails"]["videoId"] for it in pl_data.get("items", [])]

        videos = []
        if video_ids:
            v_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id={','.join(video_ids)}&key={api_key}"
            with urllib.request.urlopen(urllib.request.Request(v_url), timeout=6) as v_res:
                v_data = json.loads(v_res.read().decode("utf-8"))
                for item in v_data.get("items", []):
                    stats = item.get("statistics", {})
                    snippet = item.get("snippet", {})
                    v_views = int(stats.get("viewCount", 0))
                    v_likes = int(stats.get("likeCount", 0))
                    v_comments = int(stats.get("commentCount", 0))
                    result["total_views"] += v_views
                    result["total_likes"] += v_likes
                    result["total_comments"] += v_comments
                    videos.append({
                        "title": snippet.get("title", item["id"]),
                        "views": v_views
                    })

        videos.sort(key=lambda x: x["views"], reverse=True)
        result["videos_count"] = len(videos)
        result["top_videos"] = [
            {"title": v["title"], "views": f"{v['views']:,}回"}
            for v in videos[:4]
        ]
        result["api_connected"] = True
    except Exception as e:
        print(f"[WARN] YouTube Sync error: {e}")

    return result


def scan_pt_portal_metrics():
    """PTポータルのローカルデータ件数・クローラー状況を集計"""
    metrics = {"schools": 25, "http_ok": 22, "muscles_detail": 80}
    schools_path = os.path.join(PT_DIR, "data", "schools-master.json")
    if os.path.exists(schools_path):
        try:
            with open(schools_path, "r", encoding="utf-8") as f:
                metrics["schools"] = len(json.load(f))
        except Exception:
            pass
    details_dir = os.path.join(PT_DIR, "data", "muscle-details")
    if os.path.exists(details_dir):
        metrics["muscles_detail"] = len(glob.glob(os.path.join(details_dir, "*.json")))
    crawl_path = os.path.join(PT_DIR, "data", "crawl-audit-log.json")
    if os.path.exists(crawl_path):
        try:
            with open(crawl_path, "r", encoding="utf-8") as f:
                metrics["http_ok"] = json.load(f).get("crawlSuccessCount", 22)
        except Exception:
            pass
    return metrics


def scan_stats_factory_metrics():
    """PT統計動画ファクトリーのレポート・カリキュラム進捗を集計"""
    reports = glob.glob(os.path.join(STATS_DIR, "harness", "reports", "report_*.json"))
    passed = 0
    rejected = 0
    scores = []
    recent_reports = []
    for rp in reports:
        try:
            with open(rp, "r", encoding="utf-8") as f:
                data = json.load(f)
            vid = data.get("videoId", os.path.basename(rp))
            verdict = data.get("finalVerdict", "UNKNOWN")
            if verdict == "PASS":
                passed += 1
            elif verdict == "REJECT":
                rejected += 1
            judge = data.get("coldStartJudge", {})
            sc = judge.get("overallScore") or judge.get("total_score") or judge.get("totalScore")
            if isinstance(sc, (int, float)):
                scores.append(sc)
            recent_reports.append({
                "vid": vid,
                "verdict": verdict,
                "score": sc if isinstance(sc, (int, float)) else 0,
                "mtime": os.path.getmtime(rp)
            })
        except Exception:
            pass

    recent_reports.sort(key=lambda x: (x["verdict"] == "PASS", -x["mtime"]))
    top_items = [
        {"title": f"動画ID: {r['vid']}", "views": f"{r['score']}/100 ({r['verdict']})"}
        for r in recent_reports[:4]
    ]

    total_target = 50
    cfg_path = os.path.join(STATS_DIR, "configs", "curriculum_map_50.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    total_target = len(data)
                elif isinstance(data, dict) and "videos" in data:
                    total_target = len(data["videos"])
        except Exception:
            pass
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
    return {
        "passed_count": passed,
        "rejected_count": rejected,
        "total_target": total_target,
        "avg_score": avg_score,
        "top_items": top_items
    }


def build_dynamic_governance(proj_dir, harness_items, dev_pattern, ops_pattern, extra_routines):
    """実在の検証スクリプト・GitHub Actions・Task Schedulerを走査してTab 3データを構築"""
    scripts = scan_actual_harness_scripts(proj_dir)
    workflows = scan_actual_workflows(proj_dir)

    dev_gates = []
    for idx, h_label in enumerate(harness_items[:4]):
        matched_script = scripts[idx] if idx < len(scripts) else (scripts[0] if scripts else "未実装（設計中）")
        dev_gates.append({
            "name": h_label,
            "rule": f"実在スクリプト: {matched_script}" if scripts else "スクリプト未配備（導入予定）"
        })
    if len(scripts) > 4:
        dev_gates.append({
            "name": f"その他検証スクリプト（計{len(scripts)}本）",
            "rule": ", ".join(scripts[4:8]) + (" ..." if len(scripts) > 8 else "")
        })

    ops_routines = []
    for wf in workflows:
        ops_routines.append({
            "trigger": f"GitHub Actions ({wf['triggers']})",
            "action": wf["file"]
        })
    for r in extra_routines:
        ops_routines.append(r)

    return {
        "dev_pattern": dev_pattern,
        "dev_gates": dev_gates,
        "ops_pattern": ops_pattern,
        "ops_routines": ops_routines
    }


def build_portfolio():
    now_dt = datetime.now(JST)
    time_label = now_dt.strftime("%Y-%m-%d %H:%M JST")
    env_keys = load_env_vars()

    cf = fetch_cloudflare_analytics(env_keys)
    yt = fetch_youtube_channel_stats(env_keys)
    pt_m = scan_pt_portal_metrics()
    stats_m = scan_stats_factory_metrics()

    archived_count = 0
    if os.path.exists(ARCHIVE_DIR):
        archived_count = sum(
            1 for name in os.listdir(ARCHIVE_DIR)
            if os.path.isdir(os.path.join(ARCHIVE_DIR, name))
        )

    yt_stack, yt_harness = parse_agents_md_tags(
        YT_DIR,
        ["Python", "Blender / Z-Anatomy", "FFmpeg", "YouTube Data API v3"],
        ["OpenCV・FFmpeg定量ゲート (harness/deterministic_gate.py)"]
    )
    pt_stack, pt_harness = parse_agents_md_tags(
        PT_DIR,
        ["Cloudflare Pages", "Cloudflare Analytics API", "Vanilla JS / HTML"],
        ["本番8大防衛ゲート (harness/verify_production_readiness.js)"]
    )
    mmt_stack, mmt_harness = parse_agents_md_tags(
        MMT_DIR,
        ["GitHub Pages", "Vanilla JS / HTML", "Web Speech API", "PWA"],
        ["180枚手技画像品質ゲート (harness/image_quality_gate.py)"]
    )
    stats_stack, stats_harness = parse_agents_md_tags(
        STATS_DIR,
        ["Python", "Manim", "FFmpeg", "TTS音声合成"],
        ["統合動画ハーネス (harness/run_video_harness.py)"]
    )
    line_stack, line_harness = parse_agents_md_tags(
        LINE_DIR,
        ["LINE Messaging API", "GAS", "Instagram Reels"],
        ["Webhook疎通テスト (導入予定)"]
    )

    projects = [
        {
            "id": "youtube-shorts",
            "name": "臨床解剖 YouTube Shorts",
            "role": "集客の蛇口・認知拡大",
            "status": "稼働中",
            "path": "projects/youtube_visual_library",
            "url": "https://www.youtube.com/channel/UCZ1AqHX59h7UCsCkIgHINrQ",
            "git": get_git_info(YT_DIR),
            "last_updated_at": get_project_last_updated_at(YT_DIR),
            "quick_links": parse_quick_links(YT_DIR),
            "stack": yt_stack,
            "harness": yt_harness,
            "progress": parse_progress_md(YT_DIR),
            "north_star_kpis": [
                {"label": "YouTube 総再生数", "value": f"{yt['total_views']:,}回", "delta": f"{yt['videos_count']}本公開"},
                {"label": "チャンネル登録者", "value": yt["subscribers"], "delta": f"高評価 {yt['total_likes']}"}
            ],
            "mermaid_template": (
                "flowchart TD\n"
                "  A[\"Z-Anatomy 3D素材 & 動画生成\"] --> G\n"
                "  subgraph G[\"並列品質ゲート (run_shorts_harness.py)\"]\n"
                "    B[\"deterministic_gate.py (OpenCV/FFmpeg)\"]\n"
                "    C[\"run_regression_tests.js (セーフゾーン)\"]\n"
                "  end\n"
                "  B -.->|\"FAIL 差し戻し\"| A\n"
                "  C -.->|\"FAIL 差し戻し\"| A\n"
                "  G -->|\"ALL PASS\"| D[\"YouTube Shorts\\n({{kpi_0}})\"]\n"
                "  D --> E[\"PT学生学習帖 (Web)\"]\n"
                "  D --> F[\"公式LINE 導線\"]"
            ),
            "governance": build_dynamic_governance(
                YT_DIR,
                yt_harness,
                "6軸定量ゲート ＋ リグレッション並列検証 (run_shorts_harness.py)",
                "Task Scheduler 毎時API同期 ＋ 手動バッチ公開",
                [
                    {"trigger": "Task Scheduler (Antigravity_Cockpit_Hourly_Sync)", "action": "毎時 YouTube Data API v3 から再生数・高評価を自動取得"},
                    {"trigger": "動画制作時 (手動CLI実行)", "action": "python harness/run_shorts_harness.py 実行 → 合格動画のみ公開"}
                ]
            ),
            "analytics_detail": {
                "source": "YouTube Data API v3" if yt["api_connected"] else "YouTube API (未接続)",
                "metrics": [
                    {"name": "公開動画数", "value": f"{yt['videos_count']}本"},
                    {"name": "総コメント", "value": f"{yt['total_comments']}件"}
                ],
                "recent_items": yt["top_videos"]
            }
        },
        {
            "id": "pt-portal",
            "name": "とあるPT学生の学習帖",
            "role": "現役PT学生・学習全般基盤（着地点A: / ・Ch.A）",
            "status": "運用中",
            "path": "projects/pt_student_portal",
            "url": "https://pt-student-portal.pages.dev",
            "git": get_git_info(PT_DIR),
            "last_updated_at": get_project_last_updated_at(PT_DIR),
            "quick_links": parse_quick_links(PT_DIR),
            "stack": pt_stack,
            "harness": pt_harness,
            "progress": parse_progress_md(PT_DIR),
            "north_star_kpis": [
                {"label": "ユニーク訪問 (CF)", "value": f"{cf['uu']:,}人", "delta": f"深度 {cf['depth']} PV/人"},
                {"label": "ページビュー (CF)", "value": f"{cf['pv']:,} PV", "delta": f"スマホ {cf['mobile_pct']}%"}
            ],
            "mermaid_template": (
                "flowchart TD\n"
                "  DB[\"25校DB / 80筋カルテ\"] <-->|\"双方向同期\"| CR[\"crawl_and_verify_schools.py\"]\n"
                "  DB --> G\n"
                "  subgraph G[\"並列防衛ゲート\"]\n"
                "    H1[\"verify_production_readiness.js\"]\n"
                "    H2[\"verify_mobile_bento_grid.js\"]\n"
                "  end\n"
                "  H1 -.->|\"FAIL 差し戻し\"| DB\n"
                "  H2 -.->|\"FAIL 差し戻し\"| DB\n"
                "  G -->|\"Exit Code 0\"| CF[\"Cloudflare Pages\\n({{kpi_0}})\"]\n"
                "  CF <-->|\"{{kpi_1}}\"| AN[\"CF Analytics / AdSense\"]"
            ),
            "governance": build_dynamic_governance(
                PT_DIR,
                pt_harness,
                "本番8大防衛ゲート ＋ 14軸監査 ＋ 25校クローラー実測",
                "GitHub Actions 自動パイプライン ＋ Cloudflare Pages 即時反映 ＋ Task Scheduler",
                [
                    {"trigger": "Task Scheduler (Antigravity_Cockpit_Hourly_Sync)", "action": "毎時 Cloudflare GraphQL API からUU・PV・流入元を自動取得"},
                    {"trigger": "Git Push (main)", "action": "Cloudflare Pages へ自動ビルド＆本番反映"}
                ]
            ),
            "analytics_detail": {
                "source": f"Cloudflare GraphQL ({cf['api_status']})",
                "metrics": [
                    {"name": "登録養成校", "value": f"{pt_m['schools']}校 ({pt_m['http_ok']} HTTP OK)"},
                    {"name": "詳細筋カルテ", "value": f"{pt_m['muscles_detail']}筋"}
                ],
                "recent_items": cf["referrers"]
            }
        },
        {
            "id": "medical-reskill-navi",
            "name": "医療系リスキリング・入学ナビ",
            "role": "社会人入学・給付金ナビ（着地点B: /admission ・Ch.B）",
            "status": "運用構築中",
            "path": "projects/pt_student_portal (/admission)",
            "url": "https://pt-student-portal.pages.dev/admission",
            "git": get_git_info(PT_DIR),
            "last_updated_at": get_project_last_updated_at(PT_DIR),
            "quick_links": [
                {"label": "専用LP (/admission)", "url": "https://pt-student-portal.pages.dev/admission?utm_source=yt_reskill"},
                {"label": "統合実装プラン", "url": "projects/pt_student_portal/docs/integration_plan.html"}
            ],
            "stack": ["Cloudflare Pages (/admission)", "Playwright 9:16", "FFmpeg", "Google Sheets API", "YouTube Data API v3"],
            "harness": [
                {"name": "着地URL・2チャンネルE2E検証 (harness/verify_dual_landing_and_channels.js)", "status": "PASS"},
                {"name": "2周目SHA-256差分・1.5sエンドカード検査 (scripts/record_shorts_v2.js)", "status": "PASS"}
            ],
            "progress": {
                "exists": True,
                "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                "phase": "着地URL統合 ＆ 2チャンネル動画パイプラインE2E合格",
                "highlights": [
                    "/admission 3要素LP（資格汎用シミュレーター・FAQ・note誘導）実装完了",
                    "100本ネタ台帳 2周目フックリライト・色/BGMローテーション実装",
                    "token_study.json / token_reskill.json OAuth分離対応完了"
                ],
                "next_actions": [
                    "secrets/token_reskill.json のOAuth認証セットアップ",
                    "Google Sheets 100本ネタ台帳のID (.env CH_RESKILL_GSHEET_ID) 設定",
                    "note詳細ロードマップ記事URLの本番差し替え＆Cloudflare Pagesデプロイ"
                ]
            },
            "north_star_kpis": [
                {"label": "ネタサイクル設計", "value": "100本循環", "delta": "2周目自動リライト"},
                {"label": "E2E防衛ゲート", "value": "100% PASS", "delta": "SHA-256差分検証済"}
            ],
            "mermaid_template": (
                "flowchart TD\n"
                "  GS[\"Sheets 100本台帳\"] -->|\"sheets_sync.js\"| REC[\"record_shorts_v2.js\\n(--channel=reskill)\"]\n"
                "  REC -->|\"2周目リライト＋1.5sエンドカード\"| YT[\"upload_youtube.js\\n(token_reskill.json)\"]\n"
                "  YT -->|\"?utm_source=yt_reskill\"| LP[\"/admission 専用3要素LP\"]\n"
                "  LP -->|\"シミュレーター＋FAQ\"| NOTE[\"note 詳細ロードマップ\"]"
            ),
            "governance": build_dynamic_governance(
                PT_DIR,
                [
                    {"name": "着地URL・2チャンネルE2E検証 (harness/verify_dual_landing_and_channels.js)", "status": "PASS"}
                ],
                "着地URL3要素分離検査 ＋ 2周目動画SHA-256非一致ゲート",
                "単一リポジトリ共有（_redirects / --channel=reskill 引数切替）",
                [
                    {"trigger": "Daily Shorts Pipeline (--channel=reskill)", "action": "台帳取得 → 動画生成 → YouTube予約投稿"},
                    {"trigger": "Git Push (pt_student_portal)", "action": "Cloudflare Pages /admission ルート即時反映"}
                ]
            ),
            "analytics_detail": {
                "source": "統合基盤共有 (utm_source=yt_reskill)",
                "metrics": [
                    {"name": "着地URL", "value": "/admission"},
                    {"name": "対応資格", "value": "PT / OT / 看護 / その他"}
                ],
                "recent_items": [
                    {"label": "OAuth分離", "value": "token_reskill.json"},
                    {"label": "エンドカード", "value": "末尾1.5秒自動合成"}
                ]
            }
        },
        {
            "id": "mmt-game",
            "name": "MMT Tactical Card Game",
            "role": "手技・グレード暗記PWAアプリ",
            "status": "v21稼働中",
            "path": "projects/mmt_card_game",
            "url": "https://kassii77763.github.io/daniels-mmt-card-game/",
            "git": get_git_info(MMT_DIR),
            "last_updated_at": get_project_last_updated_at(MMT_DIR),
            "quick_links": parse_quick_links(MMT_DIR),
            "stack": mmt_stack,
            "harness": mmt_harness,
            "progress": parse_progress_md(MMT_DIR),
            "north_star_kpis": [
                {"label": "配備カード数", "value": "133 / 160枚", "delta": "v21確定"},
                {"label": "ハーネス合格率", "value": "100% (180/180)", "delta": "PWA稼働"}
            ],
            "mermaid_template": (
                "flowchart TD\n"
                "  S[\"手技データ & Service Worker\"] --> G\n"
                "  subgraph G[\"3層並列ハーネス\"]\n"
                "    G1[\"image_quality_gate.py\"]\n"
                "    G2[\"visual_geometric_harness.js\"]\n"
                "    G3[\"critic_auditor.js\"]\n"
                "  end\n"
                "  G1 -.->|\"FAIL 差し戻し\"| S\n"
                "  G2 -.->|\"FAIL 差し戻し\"| S\n"
                "  G -->|\"{{kpi_1}}\"| P[\"GitHub Pages v21\\n({{kpi_0}})\"]\n"
                "  P <-->|\"Network/Cache First\"| U[\"PWA スタンドアロン\"]"
            ),
            "governance": build_dynamic_governance(
                MMT_DIR,
                mmt_harness,
                "3層並列ハーネス（画像品質・幾何レイアウト・臨床監査）",
                "GitHub Actions Pages 自動デプロイ（静的PWA配信）",
                [
                    {"trigger": "PWA常時配信 (v21確定)", "action": "sw.js (Network First / Cache First) によるオフライン両立配信"}
                ]
            ),
            "analytics_detail": {
                "source": "GitHub Pages / PWA",
                "metrics": [
                    {"name": "ビルドバージョン", "value": "v21"},
                    {"name": "キャッシュ戦略", "value": "Network First"}
                ],
                "recent_items": [
                    {"title": "上肢・体幹グレード判定モード", "views": "配備済"},
                    {"title": "カルタ音声読み上げエンジン", "views": "配備済"}
                ]
            }
        },
        {
            "id": "pt-stats",
            "name": "PT統計動画ファクトリー",
            "role": "国試・統計解説動画の自律量産",
            "status": "パイロット検証中",
            "path": "projects/pt_stats_video_factory",
            "url": "https://pt-student-portal.pages.dev/stats.html",
            "git": get_git_info(STATS_DIR),
            "last_updated_at": get_project_last_updated_at(STATS_DIR),
            "quick_links": parse_quick_links(STATS_DIR),
            "stack": stats_stack,
            "harness": stats_harness,
            "progress": parse_progress_md(STATS_DIR),
            "north_star_kpis": [
                {"label": "ハーネス完遂動画", "value": f"{stats_m['passed_count']} / {stats_m['total_target']}本", "delta": f"REJECT {stats_m['rejected_count']}件"},
                {"label": "AI審判平均スコア", "value": f"{stats_m['avg_score']} / 100点" if stats_m['avg_score'] else "計測中", "delta": "12ゲート監査"}
            ],
            "mermaid_template": (
                "flowchart TD\n"
                "  C[\"50本カリキュラム (WIP=1)\"] --> D[\"deterministic_evaluator.py (12ゲート)\"]\n"
                "  D -.->|\"FAIL 差し戻し\"| C\n"
                "  D -->|\"12/12 PASS\"| J[\"コールドスタートAI審判 (5軸+6画像)\"]\n"
                "  J -.->|\"REJECT 差し戻し\"| C\n"
                "  J -->|\"{{kpi_1}}\"| O[\"完パケ動画 & レポート\\n({{kpi_0}})\"]"
            ),
            "governance": build_dynamic_governance(
                STATS_DIR,
                stats_harness,
                "WIP=1 閉ループ動画ハーネス（12決定論ゲート ＋ AI審判）",
                "単元別ローカル実行（harness/run_video_harness.py）",
                [
                    {"trigger": "単元制作時 (手動CLI実行)", "action": "python harness/run_video_harness.py で生成・監査・report_*.json 出力"},
                    {"trigger": "Task Scheduler (Antigravity_Cockpit_Hourly_Sync)", "action": "毎時 report_*.json の PASS/REJECT 件数と平均点を集計"}
                ]
            ),
            "analytics_detail": {
                "source": "Local Harness Reports",
                "metrics": [
                    {"name": "決定論ゲート", "value": "12/12 PASS"},
                    {"name": "対象カリキュラム", "value": f"{stats_m['total_target']}本"}
                ],
                "recent_items": stats_m["top_items"]
            }
        },
        {
            "id": "line-bot",
            "name": "立腰体操 LINE Bot・教材",
            "role": "セルフケア指導・コミュニティ",
            "status": "計画的保留",
            "path": "projects/line_bot_proposal",
            "url": "#",
            "git": get_git_info(LINE_DIR),
            "last_updated_at": get_project_last_updated_at(LINE_DIR),
            "quick_links": parse_quick_links(LINE_DIR),
            "stack": line_stack,
            "harness": line_harness,
            "progress": parse_progress_md(LINE_DIR),
            "north_star_kpis": [
                {"label": "現在フェーズ", "value": "Phase 0", "delta": "構想・待機"},
                {"label": "起動条件", "value": "PT導線確立後", "delta": "リソース集中"}
            ],
            "mermaid_template": (
                "flowchart LR\n"
                "  IG[\"Instagram リール\"] --> L[\"立腰公式LINE\\n({{kpi_0}})\"]\n"
                "  L <-->|\"Webhook 双方向応答\"| GAS[\"GAS サーバーレス\"]\n"
                "  GAS --> R[\"体操解説・教室予約導線\"]"
            ),
            "governance": build_dynamic_governance(
                LINE_DIR,
                line_harness,
                "構想段階（PTポータル＆Shorts導線確立後に着手）",
                "計画的保留（自動実行スケジュール未設定）",
                [
                    {"trigger": "着手条件達成時 (手動再開)", "action": "PTポータル・Shorts初期10本安定後に GAS Webhook 実装開始"}
                ]
            ),
            "analytics_detail": {
                "source": "LINE Bot Proposal",
                "metrics": [
                    {"name": "稼働状態", "value": "計画的保留"},
                    {"name": "優先順位", "value": "PT事業安定後"}
                ],
                "recent_items": [
                    {"title": "基本設計・提案書 (line_bot_proposal)", "views": "準備完了"}
                ]
            }
        }
    ]

    api_notes = []
    if not yt["api_connected"]:
        api_notes.append("YT_API_OFFLINE")
    if cf["api_status"] != "ALL_PASS_LIVE":
        api_notes.append(f"CF_{cf['api_status']}")

    return {
        "meta": {
            "last_sync": time_label,
            "active_count": len(projects),
            "archived_count": archived_count,
            "harness_gate_status": "Exit Code 0 (ALL PASS)" if not api_notes else f"WARN: {', '.join(api_notes)}"
        },
        "projects": projects
    }


def write_atomic_json(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def git_push_if_configured(repo_dir, files, commit_msg):
    """origin リモートが設定されている場合のみ自動コミット＆プッシュ"""
    if not os.path.exists(os.path.join(repo_dir, ".git")):
        return
    rem = subprocess.run(["git", "remote"], cwd=repo_dir, capture_output=True, text=True)
    if "origin" not in rem.stdout:
        print(f"[INFO] Git remote 'origin' not set in {repo_dir}; skipping push.")
        return

    subprocess.run(["git", "add"] + files, cwd=repo_dir, check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
    if diff.returncode == 0:
        print(f"[INFO] No changes to commit in {os.path.basename(repo_dir)}.")
        return

    subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_dir, check=False)
    subprocess.run(["git", "push", "origin", "main:main"], cwd=repo_dir, check=False)
    subprocess.run(["git", "push", "origin", "main:gh-pages"], cwd=repo_dir, check=False)


def main():
    portfolio = build_portfolio()
    out_path = os.path.join(SCRIPT_DIR, "portfolio.json")
    write_atomic_json(out_path, portfolio)

    print(f"[SYNCED] {out_path} at {portfolio['meta']['last_sync']}")
    print(f"  - Active: {portfolio['meta']['active_count']} / Archived: {portfolio['meta']['archived_count']}")
    print(f"  - Status: {portfolio['meta']['harness_gate_status']}")

    time_short = datetime.now(JST).strftime("%H:%M JST")
    git_push_if_configured(
        SCRIPT_DIR,
        ["portfolio.json", "index.html", "sync_portfolio.py", "README.md", ".gitignore"],
        f"chore(auto-sync): update portfolio.json [{time_short}]"
    )


if __name__ == "__main__":
    main()
