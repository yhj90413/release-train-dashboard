#!/usr/bin/env python3
"""
Release Train Dashboard - 수동 트리거 데이터 갱신 스크립트
Slack #tmap_release_train + Confluence Release Train('26) + 버전 업데이트 이력
→ data.json → GitHub push
"""

import json, os, re, subprocess, sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests beautifulsoup4")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DATA_FILE   = SCRIPT_DIR / "data.json"
LOG_DIR     = SCRIPT_DIR / "logs"

# ── Confluence 상수 ────────────────────────────────────────────────────────────
CONF_BASE       = "https://tmobi.atlassian.net"
SPACE_KEY       = "Product"
HISTORY_PAGE_ID = "2701918389"   # 버전 업데이트 이력(11.0.0 ver~)

# 연도별 RT 부모 페이지 ID
YEAR_PAGE_IDS = {
    2026: "2181201984",   # Release Train(`26)
    2025: "888604249",    # Release Train(`25)
}

def wiki_url(page_id: str) -> str:
    return f"{CONF_BASE}/wiki/spaces/{SPACE_KEY}/pages/{page_id}"


# ── 로깅 ───────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / f"{date.today()}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 설정 ───────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print("[ERROR] config.json 없음")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


# ── 날짜 파싱 ──────────────────────────────────────────────────────────────────
def parse_date(text: str, year: int) -> str | None:
    """텍스트에서 첫 번째 날짜 추출"""
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"(\d{1,2})/(\d{1,2})(?:[^/\d]|$)", text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{year}-{str(mo).zfill(2)}-{str(d).zfill(2)}"
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        return f"{year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    return None


def parse_date_end(text: str, year: int) -> str | None:
    """날짜 범위에서 마지막 날짜 추출 — 단계 종료일 기준 (예: '3/25 ~ 3/31' → 3/31)"""
    # yyyy-MM-dd / yyyy.MM.dd
    matches = list(re.finditer(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text))
    if matches:
        m = matches[-1]
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    # M/D 형식 (마지막 날짜)
    matches = list(re.finditer(r"(\d{1,2})/(\d{1,2})(?:[^/\d]|$)", text))
    if matches:
        m = matches[-1]
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{year}-{str(mo).zfill(2)}-{str(d).zfill(2)}"
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일", text)
    if m:
        return f"{year}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    return None


def extract_version(text: str) -> str | None:
    """버전 번호 추출 — 첫 세그먼트 2자리 이하만 허용 (연도 형식 제외)"""
    m = re.search(r"\bv(\d{1,2}\.\d{1,3}\.\d{1,3})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"(?<![.\d])(\d{1,2}\.\d{1,3}\.\d{1,3})(?![.\d])", text)
    return m.group(1) if m else None


# ── Confluence 공통 ────────────────────────────────────────────────────────────
def confluence_get(cfg: dict, path: str, params: dict = None) -> dict:
    base  = cfg["confluence"]["base_url"].rstrip("/")
    auth  = (cfg["confluence"]["email"], cfg["confluence"]["api_token"])
    res   = requests.get(f"{base}{path}", auth=auth, params=params or {}, timeout=20)
    return res.json()


def parse_tasks_from_cell(td) -> list[dict]:
    """
    Confluence 셀(BeautifulSoup tag)에서 과제명 + 티켓 URL 추출.
    반환: [{"name": str, "ticketUrl": str}]
    - Jira 매크로: 티켓 키 + https://...atlassian.net/browse/{key}
    - 일반 텍스트: 과제명만, ticketUrl=""
    """
    raw = []
    items = td.find_all("li")

    if items:
        for li in items:
            jira = li.find(attrs={"ac:name": "jira"})
            if jira:
                key_tag = jira.find(attrs={"ac:name": "key"})
                if key_tag:
                    key = key_tag.get_text(strip=True)
                    raw.append({"name": key, "ticketUrl": f"{CONF_BASE}/browse/{key}"})
                continue
            text = li.get_text(strip=True)
            if text and len(text) > 1:
                raw.append({"name": text, "ticketUrl": ""})
    else:
        for jira in td.find_all(attrs={"ac:name": "jira"}):
            key_tag = jira.find(attrs={"ac:name": "key"})
            if key_tag:
                key = key_tag.get_text(strip=True)
                raw.append({"name": key, "ticketUrl": f"{CONF_BASE}/browse/{key}"})
        if not raw:
            text = td.get_text(strip=True)
            if text and len(text) > 2:
                for t in text.split("\n"):
                    t = t.strip()
                    if t and len(t) > 1:
                        raw.append({"name": t, "ticketUrl": ""})

    # Jira 위젯 UUID 잔여 텍스트 제거
    cleaned = []
    for item in raw:
        name = re.sub(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}.*$",
                      "", item["name"]).strip()
        if name:
            cleaned.append({"name": name, "ticketUrl": item["ticketUrl"]})
    return cleaned


# ── Confluence: 연도별 RT 페이지 ───────────────────────────────────────────────
def get_rt_pages_by_year(cfg: dict, year: int) -> list[dict]:
    """
    연도별 RT 하위 페이지 파싱.
    반환: [{version, schedule, tasks, wikiUrl, pageId}]
    """
    parent_id = YEAR_PAGE_IDS.get(year)
    if not parent_id:
        return []

    data  = confluence_get(cfg, f"/wiki/rest/api/content/{parent_id}/child/page",
                           {"limit": 50, "expand": "body.storage"})
    pages = data.get("results", [])
    log(f"[Confluence] {year}년 RT 페이지 {len(pages)}개")

    result = []
    for p in pages:
        version = extract_version(p["title"])
        if not version:
            continue

        html     = p.get("body", {}).get("storage", {}).get("value", "")
        schedule = {"reviewRequest": None, "deployStart": None, "deployComplete": None}
        stages   = []   # 전체 진행단계 리스트 [{name, date}]
        tasks    = []

        if HAS_BS4 and html:
            soup = BeautifulSoup(html, "html.parser")
            for table in soup.find_all("table"):
                rows    = table.find_all("tr")
                headers = [c.get_text(strip=True) for c in (rows[0].find_all(["th","td"]) if rows else [])]

                # ── 일정 표 ──────────────────────────────────────────────────
                # 형식 A: [분류, 일정]       — 2컬럼
                # 형식 B: [분류, 내용, 일정] — 3컬럼 (단계명은 '내용' 컬럼)
                is_sched_A = len(headers) >= 2 and "분류" in headers[0] and "일정" in headers[1]
                is_sched_B = (len(headers) >= 3 and "분류" in headers[0]
                              and any("내용" in h for h in headers)
                              and any("일정" in h for h in headers))
                if is_sched_A or is_sched_B:
                    name_idx = next((i for i, h in enumerate(headers) if "내용" in h), 0)
                    date_idx = next((i for i, h in enumerate(headers) if "일정" in h), 1)
                    max_cells = len(headers)
                    for row in rows[1:]:
                        cells = row.find_all(["th","td"])
                        if len(cells) < 2:
                            continue
                        # rowspan 병합으로 셀 수가 줄어든 경우 인덱스를 왼쪽으로 shift
                        shift = max_cells - len(cells)
                        a_name = max(0, name_idx - shift)
                        a_date = max(0, date_idx - shift)
                        label     = cells[a_name].get_text(strip=True) if a_name < len(cells) else ''
                        date_text = cells[a_date].get_text(strip=True) if a_date < len(cells) else cells[-1].get_text(strip=True)

                        # 핵심 일정은 첫 번째 날짜, 단계 종료일은 마지막 날짜
                        d_start = parse_date(date_text, year)
                        d_end   = parse_date_end(date_text, year)
                        if not d_start:
                            continue
                        if re.search(r"심사\s*요청", label):
                            schedule["reviewRequest"] = d_start
                        elif re.search(r"배포\s*시작|출시|릴리즈", label):
                            schedule["deployStart"] = d_start
                        elif re.search(r"배포\s*완료", label):
                            schedule["deployComplete"] = d_start
                        # ★ 전체 단계 저장 — 종료일 기준
                        if label and (d_end or d_start):
                            stages.append({"name": label, "date": d_end or d_start})

                # ── 과제 표 (최종과제 = '확정' 상태) ──────────────────────
                elif any(k in " ".join(headers) for k in ["상세 내용", "Product"]):
                    detail_idx = next((i for i, h in enumerate(headers) if "상세 내용" in h), 2)
                    ticket_idx = next((i for i, h in enumerate(headers) if "티켓" in h), None)
                    status_idx = len(headers) - 1

                    for row in rows[1:]:
                        cells = row.find_all(["th","td"])
                        if len(cells) <= detail_idx:
                            continue
                        if not cells[0].get_text(strip=True).isdigit():
                            continue
                        status = cells[status_idx].get_text(strip=True) if len(cells) > status_idx else ""
                        if re.search(r"후속|연기|취소|추후|TBD", status, re.I):
                            continue
                        if "확정" not in status:
                            continue
                        name = cells[detail_idx].get_text(strip=True)
                        if name and len(name) > 2:
                            # 티켓 컬럼에서 Jira 티켓 URL 1:1 매핑
                            ticket_url = ""
                            if ticket_idx is not None and len(cells) > ticket_idx:
                                jira_tag = cells[ticket_idx].find(attrs={"ac:name": "jira"})
                                if jira_tag:
                                    key_tag = jira_tag.find(attrs={"ac:name": "key"})
                                    if key_tag:
                                        key = key_tag.get_text(strip=True)
                                        ticket_url = f"{CONF_BASE}/browse/{key}"
                            tasks.append({"name": name, "ticketUrl": ticket_url})

        result.append({
            "version":  version,
            "schedule": schedule,
            "stages":   stages,
            "tasks":    tasks,
            "wikiUrl":  wiki_url(p["id"]),
            "pageId":   p["id"],
        })

    return result


# ── Confluence: 버전 업데이트 이력 (패치 버전) ────────────────────────────────
def get_version_history_page(cfg: dict, year: int) -> list[dict]:
    """
    '버전 업데이트 이력(11.0.0 ver~)' 페이지에서 패치 버전 정보 추출.
    반환: [{version, schedule, tasks, wikiUrl}]
    """
    data    = confluence_get(cfg, f"/wiki/rest/api/content/{HISTORY_PAGE_ID}",
                             {"expand": "body.storage"})
    html    = data.get("body", {}).get("storage", {}).get("value", "")
    if not html or not HAS_BS4:
        return []

    soup    = BeautifulSoup(html, "html.parser")
    result  = []
    page_url = wiki_url(HISTORY_PAGE_ID)

    for table in soup.find_all("table"):
        rows    = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th","td"])]

        # 버전No. | 심사요청일 | 배포완료일 | 업데이트 내용 | 비 고
        if "버전" not in headers[0] and "버전No" not in headers[0]:
            continue

        for row in rows[1:]:
            cells = row.find_all(["th","td"])
            if len(cells) < 3:
                continue

            version = extract_version(cells[0].get_text(strip=True))
            if not version:
                continue

            # 심사요청일 / 배포완료일
            review_text   = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            complete_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""

            review_date   = parse_date(review_text, year)
            complete_date = parse_date(complete_text, year)

            # 업데이트 내용 (col 3)
            tasks = []
            if len(cells) > 3:
                tasks = parse_tasks_from_cell(cells[3])

            result.append({
                "version":  version,
                "schedule": {
                    "reviewRequest":  review_date,
                    "deployStart":    None,
                    "deployComplete": complete_date,
                },
                "tasks":   tasks,
                "wikiUrl": page_url,
            })

    log(f"[Confluence] 버전 이력 페이지에서 {len(result)}개 버전 추출")
    return result


# ── Slack ──────────────────────────────────────────────────────────────────────
def get_slack_messages(cfg: dict) -> list:
    token   = cfg["slack"]["token"]
    channel = cfg["slack"].get("channel", "tmap_release_train").lstrip("#")
    headers = {"Authorization": f"Bearer {token}"}

    # 채널 ID
    ch_id = None
    for types in ("public_channel", "private_channel"):
        res = requests.get("https://slack.com/api/conversations.list", headers=headers,
                           params={"limit": 1000, "types": types}, timeout=15)
        ch_id = next((c["id"] for c in res.json().get("channels", []) if c["name"] == channel), None)
        if ch_id:
            break

    if not ch_id:
        log(f"[Slack] #{channel} 채널 없음")
        return []

    # 메시지 수집 (최근 300개)
    all_msgs, cursor = [], None
    while len(all_msgs) < 300:
        params = {"channel": ch_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = requests.get("https://slack.com/api/conversations.history",
                            headers=headers, params=params, timeout=15).json()
        if not data.get("ok"):
            break
        all_msgs.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    log(f"[Slack] 메시지 {len(all_msgs)}개 수집")
    return all_msgs


def parse_slack_schedules(messages: list, year: int) -> dict:
    """Slack 메시지에서 버전별 일정 추출"""
    schedules: dict[str, dict] = {}

    REVIEW_KW = ["심사요청", "심사 요청"]
    DEPLOY_KW = ["배포 시작", "배포시작", "출시"]
    DONE_KW   = ["배포 완료", "배포완료"]

    for msg in messages:
        ts = float(msg.get("ts", "0"))
        if datetime.fromtimestamp(ts).year != year:
            continue

        for line in msg.get("text", "").replace("\\n", "\n").split("\n"):
            # "• 11.3.0 : 6/4(목) 심사요청 예정" 패턴
            m = re.search(r"[•·\-\*]?\s*v?(\d{1,2}\.\d{1,3}\.\d{1,3})\s*:\s*(.+)", line)
            if not m:
                m2 = re.search(r"(?<![.\d])v?(\d{1,2}\.\d{1,3}\.\d{1,3})(?![.\d])", line)
                if not m2:
                    continue
                ver, rest = m2.group(1), line
            else:
                ver, rest = m.group(1), m.group(2)

            if ver not in schedules:
                schedules[ver] = {"reviewRequest": None, "deployStart": None, "deployComplete": None}

            for part in re.split(r"→|->|~", rest):
                d = parse_date(part, year)
                if not d:
                    continue
                if any(k in part for k in DONE_KW):
                    schedules[ver]["deployComplete"] = d
                elif any(k in part for k in DEPLOY_KW):
                    schedules[ver]["deployStart"]    = d
                elif any(k in part for k in REVIEW_KW):
                    schedules[ver]["reviewRequest"]  = d
                elif not schedules[ver]["reviewRequest"]:
                    schedules[ver]["reviewRequest"]  = d

    log(f"[Slack] 버전별 일정 {len(schedules)}건 파싱")
    return schedules


# ── 데이터 병합 ────────────────────────────────────────────────────────────────
def build_rt_list(rt_pages: list, history_page: list, slack_schedules: dict) -> list[dict]:
    """RT 페이지 + 이력 페이지 + Slack 일정 병합"""
    rt_map: dict[str, dict] = {}

    # 1) 연도별 RT 메인 페이지 (11.X.0 등 정규 버전)
    for p in rt_pages:
        ver = p["version"]
        rt_map[ver] = {
            "rtNumber": f"v{ver}",
            "schedule": p["schedule"].copy(),
            "stages":   p.get("stages", []),
            "tasks":    p["tasks"],
            "wikiUrl":  p["wikiUrl"],
        }

    # 2) 버전 업데이트 이력 페이지 (패치 버전 포함)
    for p in history_page:
        ver = p["version"]
        if ver in rt_map:
            # 기존 항목 보완 (이력 페이지의 날짜·과제로 보완)
            for key in ("reviewRequest", "deployStart", "deployComplete"):
                if p["schedule"].get(key) and not rt_map[ver]["schedule"].get(key):
                    rt_map[ver]["schedule"][key] = p["schedule"][key]
            if not rt_map[ver]["tasks"] and p["tasks"]:
                rt_map[ver]["tasks"] = p["tasks"]
        else:
            rt_map[ver] = {
                "rtNumber": f"v{ver}",
                "schedule": p["schedule"].copy(),
                "stages":   [],          # 이력 페이지에는 단계 정보 없음
                "tasks":    p["tasks"],
                "wikiUrl":  p["wikiUrl"],
            }

    # 3) Slack 일정으로 보완 (배포시작·완료 등 Confluence에 없는 값)
    for ver, sched in slack_schedules.items():
        if ver not in rt_map:
            rt_map[ver] = {
                "rtNumber": f"v{ver}",
                "schedule": {"reviewRequest": None, "deployStart": None, "deployComplete": None},
                "stages":   [],
                "tasks":    [],
                "wikiUrl":  "",
            }
        for key in ("reviewRequest", "deployStart", "deployComplete"):
            if sched.get(key) and not rt_map[ver]["schedule"].get(key):
                rt_map[ver]["schedule"][key] = sched[key]

    # 버전 내림차순 정렬 + 일정 없는 항목 제거
    def ver_key(rt):
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", rt["rtNumber"])
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    sorted_list = sorted(rt_map.values(), key=ver_key, reverse=True)
    return [rt for rt in sorted_list if any(rt["schedule"].values())]


def split_current_and_history(rt_list: list) -> tuple:
    """심사요청일 <= 오늘 이면서 아직 배포 완료 안 된 가장 최신 버전 = 현재 RT"""
    today = date.today()
    current_idx = None

    for i, rt in enumerate(rt_list):
        review = rt["schedule"].get("reviewRequest")
        if not review:
            continue
        try:
            if date.fromisoformat(review) > today:
                continue
            complete = rt["schedule"].get("deployComplete")
            if complete and date.fromisoformat(complete) < today:
                continue  # 이미 완료
            current_idx = i
            break
        except ValueError:
            pass

    if current_idx is None:
        current_idx = 0

    current = rt_list[current_idx]
    others  = rt_list[:current_idx] + rt_list[current_idx + 1:]
    return current, others


# ── data.json 저장 ─────────────────────────────────────────────────────────────
def update_data_json(current_rt: dict | None, history: list) -> dict:
    existing = {}
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    ex_cur = existing.get("currentRT", {})

    new_data = {
        "lastUpdated": datetime.now().isoformat(timespec="seconds"),
        "currentRT": {
            "rtNumber": (current_rt or {}).get("rtNumber") or ex_cur.get("rtNumber", ""),
            "schedule": (current_rt or {}).get("schedule") or ex_cur.get("schedule", {}),
            "stages":   (current_rt or {}).get("stages")   or ex_cur.get("stages", []),
            "tasks":    (current_rt or {}).get("tasks")    or ex_cur.get("tasks", []),
            "wikiUrl":  (current_rt or {}).get("wikiUrl")  or ex_cur.get("wikiUrl", ""),
        },
        "history": history if history else existing.get("history", []),
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)

    log(f"[OK] data.json 저장 — 현재: {new_data['currentRT']['rtNumber']}, "
        f"히스토리: {len(new_data['history'])}건")
    return new_data


# ── Git push ───────────────────────────────────────────────────────────────────
def git_push():
    try:
        os.chdir(SCRIPT_DIR)
        subprocess.run(["git", "add", "data.json"], check=True, capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            log("[Git] 변경 없음 - push 생략")
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "commit", "-m", f"auto: data update {now}"],
                       check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"],
                       check=True, capture_output=True)
        log("[Git] GitHub push 완료")
    except subprocess.CalledProcessError as e:
        log(f"[Git] 오류: {e.stderr.decode() if e.stderr else str(e)}")


# ── 메인 ───────────────────────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("Release Train 데이터 갱신 시작")

    cfg  = load_config()
    year = date.today().year

    # Confluence: 연도별 RT 페이지
    log("[Confluence] RT 페이지 수집...")
    rt_pages = get_rt_pages_by_year(cfg, year)

    # Confluence: 버전 업데이트 이력 (패치 버전)
    log("[Confluence] 버전 이력 페이지 수집...")
    history_entries = get_version_history_page(cfg, year)

    # Slack: 일정 정보
    log("[Slack] 메시지 수집...")
    slack_msgs      = get_slack_messages(cfg)
    slack_schedules = parse_slack_schedules(slack_msgs, year)

    # 병합
    rt_list = build_rt_list(rt_pages, history_entries, slack_schedules)
    log(f"[병합] {len(rt_list)}개 RT 항목 생성")

    if not rt_list:
        log("[WARN] RT 데이터 없음 — data.json 유지")
        return

    current_rt, history = split_current_and_history(rt_list)
    update_data_json(current_rt, history)
    git_push()

    log("갱신 완료")
    log("=" * 60)


if __name__ == "__main__":
    main()
