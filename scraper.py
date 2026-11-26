"""터미널 선석배정 현황 크롤러 모음.

각 터미널/항만 사이트마다 fetch_* 함수를 하나씩 만들고,
SOURCES 리스트에 등록하면 GUI가 자동으로 합쳐서 보여줍니다.
새 사이트를 추가하려면 같은 형태의 fetch_* 함수를 추가한 뒤
SOURCES 리스트에 이름과 함께 등록하세요.
"""

import io
import os
import re
import tempfile

import certifi
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}


_DT_FORMATS = ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
               "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# 선사코드가 DYS로 안 찍혀도 실제로는 DYS 소속/파트너 선박이라 항상 추적해야 하는 선명.
# LANE 편성이 아직 안 정해진 배를 여기에 둔다. 편성이 정해지면 VESSEL_LANES로 옮긴다.
EXTRA_SHIP_NAMES = ("SM TOKYO",)


# ---- 서비스 편성표: 선명 -> (LANE, ROUTE, 운항선사) ----
# 왜 선명이 기준인가: 터미널이 주는 필드로는 편성을 알 수 없다.
#  - 선사코드(line)는 터미널마다 다르게 찍힌다. HONOR GLORY는 신선대에서 NSL,
#    광양서부에서 DYS로 나온다. 운항선사 코드라서 용선 관계에 따라 달라진다.
#  - 항로코드(route)는 BPTC와 HJIT 페이지에 아예 없고(빈 문자열), 있는 곳도
#    DONGJIN FIDES가 'BKS1'처럼 편성표(BKS)와 미묘하게 다르게 찍힌다.
# 선명은 어느 터미널에서나 같으므로 이것만 믿는다. 예전에 route 코드로
# 행선지를 추정했다가 "맞는 배와 안 맞는 배가 섞이는" 문제로 걷어냈다.
#
# 이 표에 이름을 올리면 두 가지가 동시에 된다: (1) 추적 대상 등록,
# (2) LANE 기준 행선지 분류(gui._classify_country). 새 배는 한 줄만 추가하면 된다.
VESSEL_LANES = {
    # LANE KJK — 이하 전 ROUTE 일본행
    "DONGJIN FORTUNE":      ("KJK", "NC1", "동진상선"),
    "HEUNG-A XIAMEN":       ("KJK", "NC2", "흥아라인"),
    "HONOR GLORY":          ("KJK", "DHA", "동영해운"),
    "HONOR OCEAN":          ("KJK", "HS2", "팬오션"),
    "DONGJIN FIDES":        ("KJK", "BKS", "동진상선"),
    # LANE KCK — 이하 전 ROUTE 중국행
    "PEGASUS GLORY":        ("KCK", "ISS", "동영해운"),
    "ZHONG SHENG TUO HAI":  ("KCK", "IS2", "고려해운"),
    "PANCON GLORY":         ("KCK", "IS3", "범주해운"),
    "HE YANG":              ("KCK", "BNJ", "동영해운"),
    "YANGTZE TRADER":       ("KCK", "NKX", "장금상선"),
    # LANE CKJ — 배가 아니라 그 항차의 바운드로 갈린다(LANE_BY_BOUND 참고)
    "PEGASUS TERA":         ("CKJ", "NCK", "동영해운"),
    "PEGASUS PETA":         ("CKJ", "NCK", "동영해운"),
}

# LANE -> 행선지. 이 표에 없는 LANE은 분류하지 않는다.
LANE_COUNTRY = {"KJK": "일본", "KCK": "중국"}
# 배마다 고정이 아니라 항차 바운드(E/W)로 갈리는 LANE.
LANE_BY_BOUND = ("CKJ",)


def _norm_name(s):
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def lane_of(ship_name):
    """선명으로 (LANE, ROUTE, 운항선사)를 찾는다. 편성표에 없으면 None.

    터미널에 따라 선명 뒤에 항차나 접미사가 붙는 경우가 있어 부분일치로 본다.
    """
    name = _norm_name(ship_name)
    if not name:
        return None
    for key, info in VESSEL_LANES.items():
        if key in name:
            return info
    return None


def _is_tracked(line, ship_name, extra_codes=()):
    """선사코드가 DYS이거나, 그 터미널에서 DYS와 동일하게 취급하는 컨소시엄/파트너
    항로 코드(extra_codes)이거나, VESSEL_LANES 편성표 또는 EXTRA_SHIP_NAMES에
    있는 선명이면 추적 대상으로 본다."""
    line = (line or "").strip().upper()
    if line == "DYS" or line in extra_codes:
        return True
    if lane_of(ship_name):
        return True
    ship = _norm_name(ship_name)
    return any(_norm_name(name) in ship for name in EXTRA_SHIP_NAMES)


# ===========================================================================
# 터미널별 추적 조건 (2026-11-18 사용자 지정)
#
# 모선은 선사 사정으로 자주 바뀌므로 선명이 아니라 "항로 코드"로 가져온다. 배가 바뀌어도 항로는 그대로다.
# 표기 규칙  "자체코드(터미널코드)":  괄호 앞 = 동영해운이 쓰는 코드, 괄호 안 = 그 터미널 화면에 찍히는 코드.
#   - 가져올 때는 괄호 안(터미널) 코드로 찾는다.  괄호가 없으면 두 코드가 같다.
#   - 화면에는 "자체코드 (터미널코드)" 로 보여준다 (route_label).
# 항로를 추가·삭제하려면 아래 문자열만 고치면 된다.
# ===========================================================================
def _routes(spec):
    """'NCK NC1(KJK) ...' -> {터미널코드: 자체코드}"""
    out = {}
    for tok in spec.replace(",", " ").split():
        m = re.match(r"^([^()]+)\(([^()]+)\)$", tok)
        own, term = (m.group(1), m.group(2)) if m else (tok, tok)
        out[term.strip().upper()] = own.strip().upper()
    return out


BPTC_ROUTES = _routes("NCK NC1(KJK) NC2(KHX1) HS2 KJS2 DHA DBS(KCJ) TPX(TPX1) TP2(TPX2) BXQ PBQ(KQP) "
                      "NKX BNJ KRX KRV BIH KHP KVT KCI")                      # + 선사 DYS 전부
HPNT_ROUTES = _routes("KHX BKS(BKS1) ENS(SPX1) EN2(SPX2) BSN MSK(KMS3) AK6 AK3 AK4 BLQ(CNS2) VTX")   # + 선사 DYS 전부
GWCT_ROUTES = _routes("KHX BNJ NKX VTX")
KIT_ROUTES = _routes("BIH KVT")
HJIT_ROUTES = _routes("TIS TI2(TIS2) IHP NCK IS3(ISS) IHS IQH KHX")           # + 선사 DYS 전부
SNCT_ROUTES = _routes("KHP")
PCTC_ROUTES = _routes("IHP IHS(IHS1) PXG(PXS) TI2(TIS2)")
# E1CT 는 화면에 항로 코드가 없어 여기만 선명으로 가져온다.
E1CT_SHIP_NAMES = ("PEGASUS TERA", "PANCON HARMONY", "PANCON GLORY", "STAR EXPLORER")


def _route_hit(route, routes):
    return (route or "").strip().upper() in routes


def route_label(route, routes):
    """화면 표기: 자체코드 (터미널코드). 두 코드가 같으면 하나만. 조건표에 없는 항로는 터미널 코드 그대로."""
    code = (route or "").strip().upper()
    own = routes.get(code)
    if not code:
        return ""
    return code if not own or own == code else f"{own} ({code})"


def _is_dys(line):
    return (line or "").strip().upper() == "DYS"


BPTC_EXTRA_CODES = ("KHP", "NKX")


# 본선작업현황 화면에는 항로 칸이 없어서, 선석배정에서 추적 대상으로 잡힌 선명을 기억해 뒀다가 쓴다.
_BPTC_TRACKED_NAMES = set()

# 터미널 -> 선석 목록에 보이는 모든 선명(걸러내기 전). 본선작업현황 화면에는 있는데 여기 없는 배가 있으면
# 선석 목록의 조회 범위가 좁다는 뜻이다(2026-11-20: 어제 접안한 배가 'week' 화면에 없어 간트에서 빠졌던 일).
_ALL_NAMES = {}


def fetch_bptc(time_range="week"):
    """BPTC(부산항터미널, 신선대/감만) 선석배정 현황(T)을 가져온다.

    time_range: '3days' | 'week' | 'month'
    이 페이지는 선사코드 필드에 DYS 대신 컨소시엄/파트너 코드(KHP, NKX)가
    찍히는 경우가 있어, 서버 필터 없이 전체 선사를 받아 _is_tracked()로
    클라이언트에서 걸러낸다.
    반환: [{terminal, berth, voyage, ship_name, line, eta, etd}, ...]
    """
    url = "https://info.bptc.co.kr/Berth_status_text_servlet_sw_kr"
    referer = "https://info.bptc.co.kr/content/sw/frame/berth_status_text_frame_sw_kr.jsp"
    body = {
        "v_time": time_range,
        "ROCD": "ALL",
        "ORDER": "item1",
        "v_gu": "A",
        "v_oper_cd": "",
    }
    headers = dict(HEADERS)
    headers["Referer"] = referer

    # 'week'/'month' 는 오늘 새벽 이후 입항분부터만 준다. 어제 접안해 아직 붙어 있는 배(예: 2026-11-20 의
    # HEUNG-A HAIPHONG)는 '3days' 화면에만 있어서, 두 화면을 합친다.
    trs = []
    for rng in dict.fromkeys(("3days", time_range)):
        resp = requests.post(url, data=dict(body, v_time=rng), headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        table = BeautifulSoup(resp.text, "html.parser").find("table", class_="tabletypeC")
        if table is not None and table.find("tbody") is not None:
            trs += table.find("tbody").find_all("tr")

    rows, seen = [], set()
    _ALL_NAMES["신선대"], _ALL_NAMES["감만"] = set(), set()
    for tr in trs:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 10:
            continue
        terminal, berth, voyage, ship_name, _side, line, eta_plan, eta, _work_done, etd = cells[:10]
        _ALL_NAMES.setdefault(terminal, set()).add(_norm_name(ship_name))
        route = cells[15] if len(cells) > 15 else ""       # 열: ...|양하|선적|S/H|전배|항로|검역
        if not (_is_dys(line) or _route_hit(route, BPTC_ROUTES)):
            continue
        if (terminal, voyage, ship_name) in seen:
            continue
        seen.add((terminal, voyage, ship_name))
        _BPTC_TRACKED_NAMES.add(_norm_name(ship_name))
        rows.append({
            "source": "BPTC",
            "region": "부산",
            "terminal": terminal,
            "berth": berth,
            "voyage": voyage,
            "ship_name": ship_name,
            "line": line,
            "route": route,
            "route_label": route_label(route, BPTC_ROUTES),
            "eta": _parse_dt(eta) or _parse_dt(eta_plan),
            "etd": _parse_dt(etd),
        })
    return rows


def fetch_bptc_work_status(only_tracked=True):
    """BPTC(신선대/감만) 본선작업현황(선석별 양적하 진행률)을 가져온다.

    info.bptc.co.kr 메인 페이지 자체에 선석별 현재 작업중인 선박과
    진행률(%)이 서버 렌더링되어 있어, 별도 조회 없이 그대로 파싱한다.
    fetch_bptc()와 동일하게 _is_tracked()로 걸러낸다.
    반환: [{terminal, berth, voyage, ship_name, line, eta, etd, progress,
            discharge, load}, ...]
    """
    url = "https://info.bptc.co.kr/content/index.jsp"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for dl in soup.select("div.ship-core dl"):
        dt = dl.find("dt")
        terminal = dt.get_text(strip=True) if dt else ""
        for li in dl.select("li.ship-li"):
            order = li.select_one("span.order")
            berth = order.get_text(strip=True).replace("선석 No.", "").strip() if order else ""

            progress_span = li.select_one("span.progress")
            progress_text = progress_span.get_text(strip=True) if progress_span else "0"
            progress = int(re.sub(r"[^\d]", "", progress_text) or 0)

            title = li.select_one(".state-wr .title")
            voyage = title.get_text(strip=True) if title else ""

            ship_link = li.select_one(".txt1-wr a")
            ship_full = ship_link.get_text(strip=True) if ship_link else ""
            m = re.match(r"(.+?)\((\w+)\)\s*$", ship_full)
            ship_name, line = (m.group(1).strip(), m.group(2)) if m else (ship_full, "")

            times = li.select(".txt1-wr.shiptime span")
            eta = _parse_dt(times[0].get_text(strip=True)) if len(times) > 0 else None
            etd = _parse_dt(times[1].get_text(strip=True)) if len(times) > 1 else None

            counts = li.select(".txt2-wr.operator span")
            discharge = counts[0].get_text(strip=True) if len(counts) > 0 else ""
            load = counts[1].get_text(strip=True) if len(counts) > 1 else ""

            if only_tracked and not (_is_dys(line) or _norm_name(ship_name) in _BPTC_TRACKED_NAMES):
                continue

            rows.append({
                "terminal": terminal,
                "berth": berth,
                "voyage": voyage,
                "ship_name": ship_name,
                "line": line,
                "eta": eta,
                "etd": etd,
                "progress": progress,
                "discharge": discharge,
                "load": load,
            })
    return rows


HJIT_EXTRA_CODES = ("IQH", "IHP")
HJIT_PAGE_LIMIT = 20   # 페이지 루프 안전장치. Total 파싱이 틀려도 여기서 멈춘다.


HJIT_BASE = "https://esvc2.hjit.co.kr"
HJIT_STATUS_PAGE = f"{HJIT_BASE}/HJIT/esvc/vessel/vesselStatus"


def _hjit_session():
    """새 HJIT e-서비스는 세션 쿠키 + CSRF 토큰(메타태그)이 있어야 JSON API 를 준다."""
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(HJIT_STATUS_PAGE, timeout=20)
    if resp.status_code >= 500:
        raise RuntimeError(f"터미널 서버 장애(HTTP {resp.status_code}) - 한진인천 e-서비스가 응답하지 않습니다")
    resp.raise_for_status()
    m = re.search(r'name="_csrf"\s+content="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("한진인천 e-서비스 CSRF 토큰을 찾지 못했습니다(페이지 구조 변경?)")
    hm = re.search(r'name="_csrf_header"\s+content="([^"]+)"', resp.text)
    session.headers.update({
        (hm.group(1) if hm else "X-CSRF-TOKEN"): m.group(1),
        "Referer": HJIT_STATUS_PAGE,
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


def _hjit_schedule(session, from_day, to_day):
    """/HJIT/berth/vesselSchedule : 기간 내 모든 본선(선사코드 carrier 포함)."""
    now = datetime.now()
    body = {
        "fromDate": (now + timedelta(days=from_day)).strftime("%Y%m%d"),
        "toDate": (now + timedelta(days=to_day)).strftime("%Y%m%d"),
        "statusCodes": ["A", "B", "W", "D", "P"],
    }
    resp = session.post(f"{HJIT_BASE}/HJIT/berth/vesselSchedule", json=body, timeout=20)
    resp.raise_for_status()
    return resp.json().get("vesselSchedules") or []


def _hjit_tracked(v):
    # 선사(carrier)가 DYS 이거나 항로(serviceLane)가 조건표에 있으면
    return _is_dys(v.get("carrier")) or _route_hit(v.get("serviceLane"), HJIT_ROUTES)


def _hjit_voyage(v):
    inv, outv = (v.get("inVoyage") or "").strip(), (v.get("outVoyage") or "").strip()
    code = f"{v.get('vesselCode') or ''}{v.get('voyageSeq') or ''}"
    return f"{code} {inv}/{outv}".strip() if (inv or outv) else code


def fetch_hjit(period_days=14, base_date=None):
    """HJIT(한진인천컨테이너터미널) 선석배정현황.

    2026-11 부터 e-서비스가 esvc2.hjit.co.kr 로 바뀌었다(옛 59.17.254.10:9130 은 503).
    새 사이트는 선석배정 페이지(berthScheduleG)가 쓰는 JSON API 를 그대로 호출한다.
    선사코드(carrier)가 들어 있어 DYS 를 바로 거를 수 있고, 컨소시엄 항로(IQH/IHP)도 본다.
    반환: [{source, region, terminal, berth, voyage, ship_name, line, route, eta, etd}, ...]
    """
    session = _hjit_session()
    rows = []
    for v in _hjit_schedule(session, -3, period_days):
        if not _hjit_tracked(v):
            continue
        rows.append({
            "source": "HJIT",
            "region": "인천",
            "terminal": "한진신항",
            "berth": (v.get("berthNo") or "").strip(),
            "voyage": _hjit_voyage(v),
            "ship_name": (v.get("vesselName") or "").strip(),
            "line": (v.get("carrier") or "").strip(),
            "route": (v.get("serviceLane") or "").strip(),
            "route_label": route_label(v.get("serviceLane"), HJIT_ROUTES),
            "eta": _parse_dt(v.get("atb") or v.get("etb")),
            "etd": _parse_dt(v.get("atd") or v.get("etd")),
        })
    return rows


def fetch_hjit_work_status(only_tracked=True):
    """HJIT 본선작업현황(선석별 양적하 진행률).

    vesselStatus 화면과 같은 계산: /HJIT/document/vesselContainer 로 컨테이너 목록을 받아
    QC 번호가 있는 건만 세고, psituation C(완료)/P(계획) 로 나눠 진행률 = 완료/(완료+계획).
    추적 모선(DYS·IQH/IHP·편성표)의 오늘 작업 상황만. 반환 형식은 fetch_bptc_work_status 와 같고
    status(작업중/접안완료/접안예정)가 추가된다.
    """
    session = _hjit_session()
    # 오늘 일정 중 인천항 탭에서 추적하는 모선(DYS·IQH/IHP 항로·편성표 선박)만.
    # 작업중/접안완료는 진행률을, 접안예정은 '대기'로 미리 보여준다. 다른 선사 본선은 가져오지 않는다.
    prio = {"Working": 0, "Berthed": 1, "Planned": 2}
    todays = [v for v in _hjit_schedule(session, -1, 0) if v.get("status") in prio
              and (_hjit_tracked(v) or (not only_tracked and v.get("status") != "Planned"))]
    todays.sort(key=lambda v: (prio[v["status"]], (v.get("berthNo") or ""), v.get("etb") or ""))
    rows = []
    for v in todays:
        voyage = f"{v.get('voyageSeq')}/{v.get('voyageYear')}"
        cd = cl = pd = pl = 0
        try:
            resp = session.post(f"{HJIT_BASE}/HJIT/document/vesselContainer",
                                json={"vessel": v.get("vesselCode"), "voyage": voyage, "inOutCodes": ["D", "L"]},
                                timeout=30)
            resp.raise_for_status()
            for c in resp.json().get("containers") or []:
                if not (c.get("craneNo") or "").strip():
                    continue                      # QC 미배정은 화면에서도 제외
                done = c.get("psituation") == "C"
                if c.get("disLoad") == "D":
                    cd += done; pd += (not done)
                else:
                    cl += done; pl += (not done)
        except Exception:
            pass                                  # 물량 조회 실패해도 선박은 0% 로 표시
        total = cd + cl + pd + pl
        rows.append({
            "terminal": "한진신항",
            "status": {"Working": "작업중", "Berthed": "접안완료", "Planned": "접안예정"}[v["status"]],
            "berth": (v.get("berthNo") or "").strip(),
            "voyage": _hjit_voyage(v),
            "ship_name": (v.get("vesselName") or "").strip(),
            "line": (v.get("carrier") or "").strip(),
            "eta": _parse_dt(v.get("atb") or v.get("etb")),
            "etd": _parse_dt(v.get("atd") or v.get("etd")),
            "progress": int(round((cd + cl) / total * 100)) if total else 0,
            "discharge": f"{cd}/{cd + pd}" if total else str(v.get("dischargeQty") or ""),
            "load": f"{cl}/{cl + pl}" if total else str(v.get("loadQty") or ""),
        })
    return rows


_GWCT_INFO = {}      # 모선항차 -> (선사, 접안, 출항). 본선작업현황 화면에는 선사·시각이 없어 여기서 찾는다(work.py)


def fetch_gwct(time_range="week"):
    """GWCT(광양항서부컨테이너터미널) 선석배정 목록을 가져온다.

    time_range: '3days' | 'week' | 'month'
    반환: [{terminal, berth, voyage, ship_name, line, eta, etd}, ...]

    이 사이트는 선사코드로 서버측 필터링을 지원하지 않아(폼에 필드 없음)
    전체 목록을 받아온 뒤 클라이언트에서 _is_tracked()로 걸러낸다.
    """
    base = "http://www.gwct.co.kr"
    page_url = f"{base}/sub/sub_B2"
    search_url = f"{base}/sub/sub_B2/search"

    session = requests.Session()
    session.headers.update(HEADERS)

    page_resp = session.get(page_url, timeout=15)
    page_resp.raise_for_status()
    csrf = re.search(r'name="_csrf" value="([^"]+)"', page_resp.text)
    if not csrf:
        return []
    csrf = csrf.group(1)

    # 'week' 은 오늘 입항분부터만 준다(어제 접안해 아직 붙어 있는 배가 빠진다). 기간 지정으로 사흘 전부터 받는다.
    span = {"3days": 3, "week": 8, "month": 31}.get(time_range, 8)
    d0, d1 = datetime.now() - timedelta(days=3), datetime.now() + timedelta(days=span)
    body = {
        "_csrf": csrf,
        "page": "1",
        "pageSize": "300",
        "v_time": "term",
        "fromY": str(d0.year), "fromM": f"{d0.month:02d}", "fromD": f"{d0.day:02d}",
        "toY": str(d1.year), "toM": f"{d1.month:02d}", "toD": f"{d1.day:02d}",
        "range": "ETB",
    }
    resp = session.post(search_url, data=body,
                         headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"},
                         timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="AA_list")
    if table is None:
        return []

    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 9:
            continue
        _no, berth, voyage, ship_name, _line_voyage, _side, line, eta, etd = cells[:9]
        route = cells[16] if len(cells) > 16 else ""
        _GWCT_INFO[voyage] = (line, _parse_dt(eta.replace("/", "-")), _parse_dt(etd.replace("/", "-")))
        _ALL_NAMES.setdefault("광양서부", set()).add(_norm_name(ship_name))
        if not _route_hit(route, GWCT_ROUTES):
            continue
        rows.append({
            "source": "GWCT",
            "region": "광양",
            "terminal": "광양서부",
            "berth": berth,
            "voyage": voyage,
            "ship_name": ship_name,
            "line": line,
            "route": route,
            "route_label": route_label(route, GWCT_ROUTES),
            "eta": _parse_dt(eta.replace("/", "-")),
            "etd": _parse_dt(etd.replace("/", "-")),
        })
    return rows


def fetch_gwct_empty_status(carrier_code="DYS"):
    """GWCT(광양항서부컨테이너터미널) On-Dock 선사별 컨테이너 SUMMARY에서
    DEPOT(장치장 보관) 칸의 GP(일반) 행만 뽑아온다.

    이 페이지는 선사코드로 서버측 필터링을 지원한다(ptnrCd). CSRF 토큰은
    <meta name="_csrf">에 있고, common.js가 모든 AJAX 요청에 X-CSRF-TOKEN
    헤더로 자동 첨부하는 걸 그대로 흉내낸다.
    표 헤더는 구분(행: GP/RF/OT/PL/TK/계) x [IMPORT/EXPORT/T·S/DEPOT](각
    20G/40G/40H x 2) 구조라, DEPOT 블록(뒤에서 3번째 6칸)만 골라
    20G/40G/40H 두 칸(예: S/D)을 더해 합계를 낸다.
    fetch_bptc_empty_status()와 같은 모양({carrier, terminal, counts, total})
    으로 반환해 공컨현황 표에 그대로 이어붙일 수 있게 한다.
    """
    base = "http://www.gwct.co.kr"
    page_url = f"{base}/sub/sub_F1"
    search_url = f"{base}/sub/sub_F1/search"

    session = requests.Session()
    session.headers.update(HEADERS)

    page_resp = session.get(page_url, timeout=15)
    page_resp.raise_for_status()
    csrf = re.search(r'name="_csrf" content="([^"]+)"', page_resp.text)
    if not csrf:
        return []
    csrf = csrf.group(1)

    resp = session.post(search_url, data={"ptnrCd": carrier_code},
                         headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"},
                         timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    # 행 구분 -> counts 키 접두어. RF(냉동)는 국내 터미널 중 이 페이지만 타입별로
    # 준다(BPTC의 "FL/PL/FC/FR"은 플랫랙, E1CT/HJIT는 GP/HQ만). 4H 는 40'HC.
    row_keys = {"GP": ("GP20", "GP40", "HQ40"),
                "RF": ("RF20", "RF40", "RH40"),
                "OT": ("OT20", "OT40", "OH40")}
    counts = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        kind = tds[0].get_text(strip=True) if tds else ""
        if kind not in row_keys:
            continue
        cells = [td.get_text(strip=True) for td in tds]
        if len(cells) < 27:
            continue
        depot = [int(c) if c.isdigit() else 0 for c in cells[19:25]]  # 20G-S,D 40G-S,D 40H-S,D
        k20, k40, k4h = row_keys[kind]
        counts[k20] = depot[0] + depot[1]
        counts[k40] = depot[2] + depot[3]
        counts[k4h] = depot[4] + depot[5]
    if "GP20" not in counts:
        return []
    return [{
        "carrier": carrier_code.upper(),
        "terminal": "광양서부",
        "counts": counts,
        "total": sum(counts.values()),
    }]


SNCT_BASE = "https://snct.sun-kwang.co.kr/infoservice/webpage"


def fetch_snct(line_code="KHP", days=21):
    """SNCT(선광신컨테이너터미널) 선석배정현황(텍스트)에서 항로(ROUTE)가 line_code 인 모선만.

    SNCT 는 선사코드가 아니라 항로 코드로 구분한다. 텍스트 화면은 기간과 항로를 서버에서 걸러 주므로
    (그래픽 화면은 7일씩만 보여줬다) 한 번에 3주치를 받는다.
    열: NO|선석|모선항차(+입/출항차)|년도|항로|선박명(+Bitt)|접안(예정)|반입시작|반입마감|출항(예정)|선사|양하|적하|Shift
    """
    url = f"{SNCT_BASE}/vessel/vslScheduleText.jsp"
    today = datetime.now()
    rows, seen = [], set()
    for page in range(1, 6):
        resp = requests.post(url, data={"isSearch": "Y", "page": str(page), "ROUTE": line_code,
                                        "strdStDate": (today - timedelta(days=3)).strftime("%Y-%m-%d"),
                                        "strdEdDate": (today + timedelta(days=days)).strftime("%Y-%m-%d")},
                             headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        added = 0
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 11:
                continue
            parts = [list(td.stripped_strings) for td in tds]
            first = lambda i: parts[i][0] if parts[i] else ""
            route = first(4)
            if route != line_code:
                continue
            eta = _parse_dt(first(6).strip("()"))
            etd = _parse_dt(first(9).strip("()"))
            if not eta or not etd:
                continue
            ship_name = first(5)
            voyage = parts[2][1] if len(parts[2]) > 1 else first(2)      # 입/출항차(2616N/2617S). 없으면 모선항차
            key = (ship_name, voyage, eta, etd)
            if key in seen:
                continue
            seen.add(key)
            added += 1
            rows.append({
                "source": "SNCT", "region": "인천", "terminal": "선광신항",
                "berth": first(1).split("(")[0].strip(),
                "voyage": voyage, "ship_name": ship_name,
                "line": first(10), "route": route, "route_label": route_label(route, SNCT_ROUTES),
                "eta": eta, "etd": etd,
            })
        if not added:          # 페이지가 더 없거나 같은 내용이 반복되면 끝
            break
    return rows


def fetch_snct_empty_status(carrier_code="DYS"):
    """SNCT 첫 화면의 'Empty 반출 가능 개수' 표에서 Operator 가 carrier_code 인 행.
    열: Operator | DRY 20,40,40H,45 | RF 20,40 | FR 20,40 | OT 20,40 | TK 20,40   (Damage 구분은 없다)"""
    resp = requests.get(f"{SNCT_BASE}/main/_fetchEmptyData_new.jsp", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    keys = ("GP20", "GP40", "HQ40", "HQ45", "RF20", "RH40", "FL20", "FL40", "OT20", "OT40", "TK20", "TK40")
    soup = BeautifulSoup("<table>" + resp.text + "</table>", "html.parser")
    found = False
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 13:
            continue
        found = True
        if cells[0].upper() != carrier_code.upper():
            continue
        counts = {k: (int(c.replace(",", "")) if c.replace(",", "").isdigit() else 0) for k, c in zip(keys, cells[1:13])}
        return [{"carrier": carrier_code.upper(), "terminal": "선광신항", "counts": counts, "total": sum(counts.values())}]
    if not found:
        raise ValueError("SNCT Empty 표를 읽지 못했습니다")
    return [{"carrier": carrier_code.upper(), "terminal": "선광신항",          # 표는 있는데 우리 선사 행이 없으면 재고 0
             "counts": {"GP20": 0, "GP40": 0, "HQ40": 0}, "total": 0}]


# --------------------------------------------------------------------------
# E1CT 는 2026-09 경 HTTPS 로 전환(http 요청은 301 리다이렉트)했는데, 서버가
# 중간 인증서를 함께 보내지 않는다. 브라우저는 AIA 로 알아서 받아오지만
# requests/OpenSSL 은 그러지 않아 "unable to get local issuer certificate" 로
# 검증이 실패한다. 그래서 발급자인 Sectigo 중간 인증서를 certifi 번들 뒤에
# 붙여서 쓴다 — verify=False 로 검증을 끄지 않기 위한 조치다.
#
# 이 인증서 유효기간: ~2036-03-21. 언젠가 E1CT 가 CA 를 바꾸면 다시 실패하는데,
# 그때는 http://crt.sectigo.com/... (leaf 인증서의 AIA 필드에 적힌 주소)에서
# 새 중간 인증서를 받아 아래 문자열만 교체하면 된다.
E1CT_INTERMEDIATE_PEM = """-----BEGIN CERTIFICATE-----
MIIGTDCCBDSgAwIBAgIQLBo8dulD3d3/GRsxiQrtcTANBgkqhkiG9w0BAQwFADBf
MQswCQYDVQQGEwJHQjEYMBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTYwNAYDVQQD
Ey1TZWN0aWdvIFB1YmxpYyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gUm9vdCBSNDYw
HhcNMjEwMzIyMDAwMDAwWhcNMzYwMzIxMjM1OTU5WjBgMQswCQYDVQQGEwJHQjEY
MBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMTcwNQYDVQQDEy5TZWN0aWdvIFB1Ymxp
YyBTZXJ2ZXIgQXV0aGVudGljYXRpb24gQ0EgT1YgUjM2MIIBojANBgkqhkiG9w0B
AQEFAAOCAY8AMIIBigKCAYEApkMtJ3R06jo0fceI0M52B7K+TyMeGcv2BQ5AVc3j
lYt76TvHIu/nNe22W/RJXX9rWUD/2GE6GF5x0V4bsY7K3IeJ8E7+KzG/TGboySfD
u+F52jqQBbY62ofhYjMeiAbLI02+FqwHeM8uIrUtcX8b2RCxF358TB0NHVccAXZc
FYgZndZCeXxjuca7pJJ20LLUnXtgXcjAE1vY4WvbReW0W6mkeZyNGdmpTcFs5Y+s
yy6LtE5Zocji9J9NlNnReox2RWVyEXpA1ChZ4gqN+ZpVSIQ0HBorVFbBKyhdZyEX
gZgNSNtBRwxqwIzJePJhYd4ZUhO1vk+/uP3nwDk0p95q/j7naXNCSvESnrHPypaB
WRK066nKfPRPi9m9kIOhMdYfS8giFRTcdgL24Ycilj7ecAK9Trh0VbjwouJ4WH+x
bt47u68ZFCD/ac55I0DNHkCpaPruj6e9Rmr7K46wZDAYXuEAqB7tGG/jd6JAA+H2
O44CV98NRsU213f1kScIZntNAgMBAAGjggGBMIIBfTAfBgNVHSMEGDAWgBRWc1hk
lfmSGrASKgRieaFAFYghSTAdBgNVHQ4EFgQU42Z0u3BojSxdTg6mSo+bNyKcgpIw
DgYDVR0PAQH/BAQDAgGGMBIGA1UdEwEB/wQIMAYBAf8CAQAwHQYDVR0lBBYwFAYI
KwYBBQUHAwEGCCsGAQUFBwMCMBsGA1UdIAQUMBIwBgYEVR0gADAIBgZngQwBAgIw
VAYDVR0fBE0wSzBJoEegRYZDaHR0cDovL2NybC5zZWN0aWdvLmNvbS9TZWN0aWdv
UHVibGljU2VydmVyQXV0aGVudGljYXRpb25Sb290UjQ2LmNybDCBhAYIKwYBBQUH
AQEEeDB2ME8GCCsGAQUFBzAChkNodHRwOi8vY3J0LnNlY3RpZ28uY29tL1NlY3Rp
Z29QdWJsaWNTZXJ2ZXJBdXRoZW50aWNhdGlvblJvb3RSNDYucDdjMCMGCCsGAQUF
BzABhhdodHRwOi8vb2NzcC5zZWN0aWdvLmNvbTANBgkqhkiG9w0BAQwFAAOCAgEA
BZXWDHWC3cubb/e1I1kzi8lPFiK/ZUoH09ufmVOrc5ObYH/XKkWUexSPqRkwKFKr
7r8OuG+p7VNB8rifX6uopqKAgsvZtZsq7iAFw04To6vNcxeBt1Eush3cQ4b8nbQR
MQLChgEAqwhuXp9P48T4QEBSksYav7+aFjNySsLYlPzNqVM3RNwvBdvp6vgDtGwc
xlKQZVuuNVIaoYyls8swhxDeSHKpRdxRauTLZ+pl+wGvy0pnrLEJGSz9mOEmfbod
e/XopR2NGqaHJ6bIjyxPu6UtyQGI26En7UAEozACrHz06Nx2jTAY9E6NeB6XuobE
wLK025ZRmvglcURG1BrV24tGHHTgxCe8M3oGlpUSMTKQ2dkgljZVYt+gKdFtWELZ
MuRdi+X3XsrR8LFz+aLUiDRfQqhmw3RxjIyVKvvu9UPYY1nsvxYmFnUSeM+2q1z/
iPUry+xDY9MC6+IhleKT094VKdFVp7LXH42+wvU+17lRolQ2mK2N/nBLVBwaIhib
QXw4VYKwB86Bc6eS6iqsc94KEgD/U4VsjmgfhK+Xp4NM+VYzTTa3QeV3p8xOM0cw
q1p8oZFA+OBcz3FYWpDIe5j0NWKlw9hXsTyPY/HeZUV59akskSOSRSmDfe8wJDPX
58uB9/7lud0G3x0pxQAcffP0ayKavNwDTw4UfJ34cEw=
-----END CERTIFICATE-----
"""

_E1CT_CA_BUNDLE = None


def _e1ct_ca_bundle():
    """certifi 기본 번들 + E1CT 중간 인증서를 합친 임시 파일 경로를 돌려준다."""
    global _E1CT_CA_BUNDLE
    if _E1CT_CA_BUNDLE and os.path.exists(_E1CT_CA_BUNDLE):
        return _E1CT_CA_BUNDLE
    try:
        path = os.path.join(tempfile.gettempdir(), "berthmonitor_e1ct_ca.pem")
        with io.open(certifi.where(), encoding="ascii") as f:
            base = f.read()
        with io.open(path, "w", encoding="ascii") as f:
            f.write(base.rstrip() + "\n" + E1CT_INTERMEDIATE_PEM)
        _E1CT_CA_BUNDLE = path
        return path
    except OSError:
        return certifi.where()      # 임시파일을 못 쓰면 기본 검증으로 되돌린다


def fetch_e1ct(only_tracked=True):
    """E1CT(인천 E1컨테이너터미널) 선석배정현황(T)을 가져온다.

    only_tracked=False 면 선명으로 거르지 않는다(본선작업현황에서 모선항차 -> 선명을 찾을 때 쓴다).

    날짜를 지정하지 않으면 오늘부터 7일치를 그대로 보여주는 페이지라
    파라미터 없이 그대로 가져온다.

    이 사이트의 HTML은 </td> 닫는 태그가 군데군데 빠져 있어(불량 마크업)
    BeautifulSoup의 td 탐색만으로는 값이 밀려서, <td 여는 태그 기준으로
    직접 정규식으로 셀을 분리한다.
    반환: [{terminal, berth, voyage, ship_name, line, eta, etd}, ...]
    """
    url = "https://www.e1ct.co.kr/info/terminal/berthText"
    resp = requests.get(url, headers=HEADERS, timeout=15,
                        verify=_e1ct_ca_bundle())
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    tbody_match = re.search(r"<tbody>(.*?)</tbody>", resp.text, re.S)
    if not tbody_match:
        return []

    rows = []
    for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", tbody_match.group(1), re.S):
        cell_chunks = re.split(r"<td[^>]*>", tr_match.group(1))[1:]
        cells = []
        for raw in cell_chunks:
            lines = [re.sub(r"<[^>]+>", "", part).strip()
                     for part in re.split(r"<br\s*/?>", raw)]
            cells.append([l for l in lines if l])
        if len(cells) < 9:
            continue

        line = cells[8][0] if cells[8] else ""
        ship_name = cells[4][0] if cells[4] else ""
        if only_tracked and not any(n in _norm_name(ship_name) for n in E1CT_SHIP_NAMES):     # 항로 코드가 없는 화면이라 선명으로
            continue

        rows.append({
            "source": "E1CT",
            "region": "인천",
            "terminal": "E1CT",
            "berth": cells[1][0].split("(")[0].strip() if cells[1] else "",
            "voyage": " ".join(cells[2]) if cells[2] else "",
            "ship_name": ship_name,
            "line": line,
            "route": "",  # E1CT 선석배정현황(T) 페이지에는 항로 코드가 없음
            "eta": _parse_dt(cells[5][0].strip("()")) if cells[5] else None,
            "etd": _parse_dt(cells[7][0].strip("()")) if cells[7] else None,
        })
    return rows


def fetch_e1ct_empty_status(carrier_code="DYS"):
    """E1CT(인천 E1컨테이너터미널) 공컨테이너 반출 가능 수량 페이지에서
    선사코드에 해당하는 행만 뽑아온다.

    이 페이지는 파라미터 없이 GET하면 전체 선사 목록이 완성된 표로
    내려온다(서버측 필터 없음). 컬럼은 20GP/40GP/40HQ/45GP/합계인데,
    45GP는 실제로 쓰이지 않는 항목이라(늘 0) 가져오지 않는다.
    fetch_bptc_empty_status()와 같은 모양으로 반환해 공컨현황 표에
    그대로 이어붙일 수 있게 한다.
    """
    url = "https://www.e1ct.co.kr/info/terminal/emptyContainer"
    resp = requests.get(url, headers=HEADERS, timeout=15,
                        verify=_e1ct_ca_bundle())
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.select_one("div.tb_tmnl table")
    if table is None or table.find("tbody") is None:
        return []

    for tr in table.find("tbody").find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 5 or cells[0].strip().upper() != carrier_code.upper():
            continue
        gp20, gp40, hq40 = (int(c) if c.isdigit() else 0 for c in cells[1:4])
        counts = {"GP20": gp20, "GP40": gp40, "HQ40": hq40}
        return [{
            "carrier": carrier_code.upper(),
            "terminal": "E1CT",
            "counts": counts,
            "total": sum(counts.values()),
        }]
    return []


def fetch_hjit_empty_status(carrier_code="DYS"):
    """HJIT(한진인천컨테이너터미널) 메인페이지의 "Empty 반출 가능 개수"에서
    선사코드에 해당하는 행만 뽑아온다.

    메인페이지가 /kor/main/prg2/getPrg2.json을 호출해 선사별 20/40/40HC/45
    반출가능 수량을 JSON으로 채워 넣는다(인증 불필요). 45(m45)는 실제로
    쓰이지 않는 항목이라 가져오지 않는다.
    fetch_bptc_empty_status()와 같은 모양으로 반환해 공컨현황 표에
    그대로 이어붙일 수 있게 한다.
    """
    url = "https://www.hjit.co.kr/kor/main/prg2/getPrg2.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    obj = resp.json()

    for o in obj.get("result", {}).get("data_list", []):
        if (o.get("opr") or "").strip().upper() != carrier_code.upper():
            continue
        counts = {
            "GP20": int(o.get("m20")) if str(o.get("m20")).isdigit() else 0,
            "GP40": int(o.get("m40")) if str(o.get("m40")).isdigit() else 0,
            "HQ40": int(o.get("m40hc")) if str(o.get("m40hc")).isdigit() else 0,
        }
        return [{
            "carrier": carrier_code.upper(),
            "terminal": "한진신항",
            "counts": counts,
            "total": sum(counts.values()),
        }]
    return []


def _find_hpnt_table(soup):
    """결과 표를 찾는다. 페이지가 표를 여러 겹으로 감싸고 있어(레이아웃용
    바깥 표들도 안쪽 표의 텍스트를 그대로 포함), '선명' 헤더 셀을 정확히
    일치시켜 그 셀이 속한 가장 안쪽 tr/table을 찾아야 한다."""
    for td in soup.find_all("td"):
        if td.get_text(strip=True) == "선명":
            tr = td.find_parent("tr")
            return tr.find_parent("table") if tr else None
    return None


_HPNT_ALL = []       # 선석배정에 보이는 모든 모선(걸러내기 전). 본선작업현황(work.py)이 접안 중인 배를 고를 때 쓴다
_KIT_ALL = []


def fetch_hpnt():
    """HPNT(부산항 허치슨터미널) 선석배정현황을 가져온다.

    반환: [{terminal, berth, voyage, ship_name, line, eta, etd}, ...]

    결과가 페이지당 20건씩 나뉘어(currentPage) 있어, 첫 페이지에서
    "전체 조회 건수"를 읽어 전체 페이지를 순회하며 모두 모은다.
    """
    url = "https://custom.hktl.com/jsp/T01/sunsuk.jsp"
    session = requests.Session()
    session.headers.update(HEADERS)

    # 기본 화면은 오늘 입항분부터만 보여 줘서 어제 접안해 아직 작업 중인 배가 빠진다. 사흘 전 날짜로 조회한다.
    since = datetime.now() - timedelta(days=3)
    since_form = {"year": str(since.year), "month": f"{since.month:02d}", "day": f"{since.day:02d}", "startPage": "1"}
    resp = session.post(url, data=dict(since_form, currentPage="1"), timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    total_match = re.search(r"전체\s*조회\s*건수:\s*(\d+)", soup.get_text())
    total_count = int(total_match.group(1)) if total_match else 20
    total_pages = max(1, -(-total_count // 20))  # 올림 나눗셈

    rows = []
    del _HPNT_ALL[:]
    for page in range(1, total_pages + 1):
        if page == 1:
            page_soup = soup
        else:
            resp = session.post(url, data=dict(since_form, currentPage=str(page)), timeout=15)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "euc-kr"
            page_soup = BeautifulSoup(resp.text, "html.parser")

        table = _find_hpnt_table(page_soup)
        if table is None:
            continue

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 14:
                continue
            (voyage_code, inout_voy, berth, _bitt, eta, _work, etd, _closing,
             _side, _qty, _qc, ship_name, line, route) = cells[:14]

            _HPNT_ALL.append({"code": voyage_code, "berth": berth, "voyage": f"{voyage_code} {inout_voy}".strip(),
                              "ship_name": ship_name, "line": line, "eta": _parse_dt(eta), "etd": _parse_dt(etd)})
            if not (_is_dys(line) or _route_hit(route, HPNT_ROUTES)):
                continue

            rows.append({
                "source": "HPNT",
                "region": "부산",
                "terminal": "허치슨",
                "berth": berth,
                "voyage": f"{voyage_code} {inout_voy}".strip(),
                "ship_name": ship_name,
                "line": line,
                "route": route,
                "route_label": route_label(route, HPNT_ROUTES),
                "eta": _parse_dt(eta),
                "etd": _parse_dt(etd),
            })
    return rows


# ---------------------------------------------------------------------------
# 광양국제 (KIT, 한국국제터미널) - 광양항.  허치슨과 같은 계열 화면(info.kitl.com)
# ---------------------------------------------------------------------------
KIT_BASE = "https://info.kitl.com/jsp"


def _kit_cells(tr_html):
    """이 사이트는 </td> 가 군데군데 빠져 있어 <td 여는 태그 기준으로 직접 자른다."""
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in re.split(r"<td[^>]*>", tr_html)[1:]]


def fetch_kit():
    """광양국제 선석배정현황에서 항로(Route)가 KIT_ROUTES 인 모선만.
    열: 터미널항차|선사항차|선석|Bitt|접안예정|출항예정|Closing|Port/STBD|총물량|QC|모선명|선사|Route|Fixed
    한 페이지 20건이고 'Total : N' 으로 전체 건수를 알려준다."""
    url = f"{KIT_BASE}/T01/sunsuk.jsp"
    session = requests.Session()
    session.headers.update(HEADERS)
    today = datetime.now() - timedelta(days=3)
    form = {"year": str(today.year), "month": f"{today.month:02d}", "day": f"{today.day:02d}", "vessel": "",
            "langType": "K", "mainType": "T01", "subType": "01", "optType": "T", "terminal": "KITLIB",
            "startPage": "1"}
    rows, seen, pages, page = [], set(), 1, 1
    del _KIT_ALL[:]
    while page <= min(pages, 15):
        resp = session.post(url, data=dict(form, currentPage=str(page)), timeout=20)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        if page == 1:
            m = re.search(r"Total\s*:\s*(?:<[^>]+>\s*)*(\d+)", resp.text)
            pages = max(1, -(-int(m.group(1)) // 20)) if m else 1
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S | re.I):
            if "routeNew(" not in tr:
                continue
            c = _kit_cells(tr)
            if len(c) < 13:
                continue
            route, ship_name = c[12], c[10]
            if _parse_dt(c[4]) and _parse_dt(c[5]) and not any(a["code"] == c[0] for a in _KIT_ALL):
                _KIT_ALL.append({"code": c[0], "berth": c[2], "voyage": f"{c[0]} {re.sub(' ', '', c[1])}".strip(),
                                 "ship_name": ship_name, "line": c[11], "eta": _parse_dt(c[4]), "etd": _parse_dt(c[5])})
            if not _route_hit(route, KIT_ROUTES):
                continue
            eta, etd = _parse_dt(c[4]), _parse_dt(c[5])
            if not eta or not etd:
                continue
            key = (c[0], eta)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "source": "KIT", "region": "광양", "terminal": "광양국제",
                "berth": c[2], "voyage": f"{c[0]} {re.sub(' ', '', c[1])}".strip(), "ship_name": ship_name,
                "line": c[11], "route": route, "route_label": route_label(route, KIT_ROUTES),
                "eta": eta, "etd": etd,
            })
        page += 1
    return rows


def fetch_kit_empty_status(carrier_code="DYS"):
    """광양국제 '선사별 공컨 반출 가능 수량'(데미지·홀딩·지정·선적·환적 제외). 열: 선사|20|40|40HQ|45"""
    resp = requests.get(f"{KIT_BASE}/T05/banchul_allow.jsp", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")
    found = False
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td", recursive=False)]
        if len(cells) != 5 or not cells[1].isdigit():
            continue
        found = True
        if cells[0].upper() != carrier_code.upper():
            continue
        counts = dict(zip(("GP20", "GP40", "HQ40", "HQ45"), (int(x) if x.isdigit() else 0 for x in cells[1:5])))
        return [{"carrier": carrier_code.upper(), "terminal": "광양국제", "counts": counts, "total": sum(counts.values())}]
    if not found:
        raise ValueError("광양국제 공컨 표를 읽지 못했습니다")
    return [{"carrier": carrier_code.upper(), "terminal": "광양국제", "counts": {"GP20": 0, "GP40": 0, "HQ40": 0}, "total": 0}]


BPTC_EMPTY_CARRIER_CODES = ("DYS",)
BPTC_EMPTY_COL_LABELS = ["GP20", "GP40", "OT20", "OT40", "FL20", "FL40", "HQ20", "HQ40", "HQ45"]


def fetch_bptc_empty_status(carrier_codes=BPTC_EMPTY_CARRIER_CODES):
    """BPTC(신선대/감만) On-Dock 공컨테이너 현황을 가져온다.

    이 페이지는 AJAX 없이 서버가 전체 선사 목록이 채워진 표를 그대로
    내려준다(필터 파라미터 없음). 한 행에 신선대 10칸 + 감만 10칸이
    나란히 붙어 있어(각 칸 = 선사코드 1 + 컨테이너 타입/사이즈별 수량 9),
    전체를 받아온 뒤 carrier_codes에 해당하는 선사만 골라낸다.
    활동이 없는 선사/터미널 조합은 원본 표에 행 자체가 없어 결과에서 빠질 수 있다.
    반환: [{carrier, terminal, counts: {GP20: n, ...}, total}, ...]
    """
    url = "https://info.bptc.co.kr/content/od/frame/yard_new_empty_frame_od_kr.jsp"
    params = {"p_id": "EMPT_NE_KR", "snb_num": "8", "snb_div": "service", "pop_ok": "Y"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "euc-kr"

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="tabletypeC")
    if table is None:
        return []

    # 이 페이지 마크업이 <tbody> 여는 태그 앞에 낙오된 <tr>가 하나 끼어 있는 등
    # 불량해서(E1CT와 비슷한 사례), BeautifulSoup가 데이터 행들을 <tbody> 안에
    # 제대로 넣지 못한다. table.find_all("tr")로 전체를 직접 순회하면
    # 헤더 행(<th>만 있어 td 0개)과 빈 낙오 행은 아래 개수 체크로 자연히 걸러진다.
    codes = tuple(c.upper() for c in carrier_codes)
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 20:
            continue
        for terminal, chunk in (("신선대", cells[0:10]), ("감만", cells[10:20])):
            carrier = chunk[0].strip().upper()
            if carrier not in codes:
                continue
            counts = {}
            total = 0
            for label, raw in zip(BPTC_EMPTY_COL_LABELS, chunk[1:]):
                val = int(raw) if raw.strip().isdigit() else 0
                counts[label] = val
                total += val
            rows.append({"carrier": carrier, "terminal": terminal, "counts": counts, "total": total})
    return rows


# ---------------------------------------------------------------------------
# PCTC (한진평택컨테이너터미널) - 평택항
# ---------------------------------------------------------------------------
PCTC_BASE = "https://www.pctc21.com"


def fetch_pctc(days=14):
    """PCTC 선석배정 목록. 화면이 부르는 JSON(/esvc/vessel/berthScheduleT/data)을 그대로 읽는다.
    선사 필터가 없어 전체를 받아 항로(IN_LANE)가 PCTC_ROUTES 조건표에 있는 것만 남긴다(선사 무관)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(f"{PCTC_BASE}/esvc/vessel/berthScheduleT", timeout=15).raise_for_status()
    start = datetime.now() - timedelta(days=3)
    end = datetime.now() + timedelta(days=days)
    rows, page = [], 1
    while page <= 10:
        resp = session.get(f"{PCTC_BASE}/esvc/vessel/berthScheduleT/data",
                           params={"startDate": start.strftime("%Y-%m-%d"), "endDate": end.strftime("%Y-%m-%d"),
                                   "sort": "ETB", "page": page},
                           headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
        resp.raise_for_status()
        j = resp.json()
        for v in j.get("content") or []:
            _ALL_NAMES.setdefault("PCTC", set()).add(_norm_name(v.get("VSL_NM")))
            lane = (v.get("IN_LANE") or "").strip().upper()
            if not _route_hit(lane, PCTC_ROUTES):
                continue
            eta, etd = _parse_dt(v.get("ATA") or ""), _parse_dt(v.get("ATD") or "")
            if not eta or not etd:
                continue
            rows.append({
                "source": "PCTC", "region": "평택", "terminal": "PCTC",
                "berth": (v.get("BERTH_NO") or "").strip(),
                "voyage": (v.get("OPR_VOY") or v.get("VOY_NO") or "").strip(),
                "ship_name": (v.get("VSL_NM") or "").strip(),
                "line": (v.get("PTNR_CODE") or "").strip(),
                "route": lane, "route_label": route_label(lane, PCTC_ROUTES), "eta": eta, "etd": etd,
            })
        if page >= int(j.get("totalPages") or 1):
            break
        page += 1
    return rows


def fetch_pctc_empty_status(carrier_code="DYS"):
    """PCTC Empty 현황(선사코드별). DAMAGE 는 빼고 SOUND 만 센다.
    화면 스크립트와 같은 기준: DMG_COND 가 'S' 이거나 비어 있으면 Sound. 크기 22=20', 42=40', 그 외(45)=40'HC."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(f"{PCTC_BASE}/esvc/yard/emptyStatus", timeout=15).raise_for_status()
    resp = session.get(f"{PCTC_BASE}/esvc/yard/emptyStatus/data", params={"shippingCode": carrier_code},
                       headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
    resp.raise_for_status()
    kinds = {"G0": ("GP20", "GP40", "HQ40"), "R0": ("RF20", "RF40", "RH40"), "U0": ("OT20", "OT40", "OH40"),
             "P0": ("FL20", "FL40", "FL40"), "P1": ("FL20", "FL40", "FL40"), "T0": ("TK20", "TK40", "TK40")}
    counts = {"GP20": 0, "GP40": 0, "HQ40": 0}
    for r in resp.json():
        if r.get("DMG_COND") not in ("S", None):
            continue
        keys = kinds.get(r.get("CON_TYPE"))
        if not keys:
            continue
        size = str(r.get("CON_SIZE") or "")
        key = keys[0] if size in ("20", "22") else keys[1] if size == "42" else keys[2]
        counts[key] = counts.get(key, 0) + int(r.get("CNT") or 0)
    return [{"carrier": carrier_code.upper(), "terminal": "PCTC", "counts": counts, "total": sum(counts.values())}]


# ---------------------------------------------------------------------------
# GCT (군산컨테이너터미널) - 군산항.  광양서부(GWCT)의 옛 e-service 와 같은 계열 화면이다.
# ---------------------------------------------------------------------------
GCT_URL = "http://www.igct.co.kr/e-service/"


def _gct_post(data):
    resp = requests.post(GCT_URL, data=data, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def fetch_gct(time_range="month", carrier_code="DYS"):
    """GCT 선석배정 목록에서 선사가 carrier_code 인 것만.
    열: 선석|모선항차|선박명|접안|선사|입항|출항|작업시작|작업완료|반입마감|양하|선적|S/H|전배|항로"""
    d0, d1 = datetime.now() - timedelta(days=3), datetime.now() + timedelta(days=31)
    soup = _gct_post({"m": "B", "s": "2", "v_time": "term", "range": "ETB", "screenPath": "", "callType": "",
                      "fromY": str(d0.year), "fromM": f"{d0.month:02d}", "fromD": f"{d0.day:02d}",
                      "toY": str(d1.year), "toM": f"{d1.month:02d}", "toD": f"{d1.day:02d}"})
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td", recursive=False)]
        if len(cells) < 15 or cells[4].upper() != carrier_code.upper():
            continue
        eta, etd = _parse_dt(cells[5]), _parse_dt(cells[6])
        if not eta or not etd:
            continue
        rows.append({
            "source": "GCT", "region": "군산", "terminal": "GCT",
            "berth": cells[0], "voyage": cells[1], "ship_name": cells[2],
            "line": cells[4], "route": cells[14], "route_label": cells[14], "eta": eta, "etd": etd,
        })
    return rows


def fetch_gct_empty_status(carrier_code="DYS"):
    """GCT 선사별 컨테이너 SUMMARY 에서 DEPOT 칸의 S(Sound) 만. D(Damage) 는 뺀다.
    한 행 = 구분 + [IMPORT|EXPORT|T/S](각 20G/40G/40H x F,E) 18칸 + DEPOT(20G/40G/40H x S,D) 6칸 + VAN,TEU."""
    soup = _gct_post({"m": "F", "s": "1", "xls_mode": "", "ptnr_code": carrier_code, "screenPath": "", "callType": ""})
    row_keys = {"GP": ("GP20", "GP40", "HQ40"), "RF": ("RF20", "RF40", "RH40"),
                "OT": ("OT20", "OT40", "OH40"), "PL": ("FL20", "FL40", "FL40")}
    counts, seen = {}, False
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td", recursive=False)]
        if len(cells) < 27 or cells[0] not in row_keys:
            continue
        seen = True
        num = lambda s: int(s.replace(",", "")) if s.replace(",", "").isdigit() else 0
        for key, idx in zip(row_keys[cells[0]], (19, 21, 23)):       # 19,21,23 = DEPOT 의 S 칸 (20,22,24 는 D)
            counts[key] = counts.get(key, 0) + num(cells[idx])
    if not seen:
        return []
    return [{"carrier": carrier_code.upper(), "terminal": "GCT", "counts": counts, "total": sum(counts.values())}]


# 여기에 사이트를 추가로 등록하세요. (이름, 함수)
SOURCES = [
    ("BPTC (신선대/감만)", fetch_bptc),
    ("허치슨 (부산항 허치슨터미널·자성대/감만)", fetch_hpnt),
    ("한진신항 (한진인천컨테이너터미널)", fetch_hjit),
    ("선광신항 (선광신컨테이너터미널)", fetch_snct),
    ("E1CT (E1컨테이너터미널)", fetch_e1ct),
    ("광양서부 (광양항서부컨테이너터미널)", fetch_gwct),
    ("광양국제 (한국국제터미널 KIT)", fetch_kit),
    ("PCTC (한진평택컨테이너터미널)", fetch_pctc),
    ("GCT (군산컨테이너터미널)", fetch_gct),
]


def with_retry(fn, tries=2, wait=3):
    """접속 자체가 안 된 경우(시간 초과·연결 실패)에만 잠깐 쉬었다 다시 시도한다.

    GitHub Actions(해외 서버)에서 국내 터미널 사이트로 가는 접속이 가끔 시간 초과된다(선광신항, BPTC 등).
    사이트가 응답은 했는데 내용이 이상한 경우는 다시 해도 같으므로 재시도하지 않는다."""
    import time
    for i in range(tries):
        try:
            return fn()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if i == tries - 1:
                raise
            time.sleep(wait)


def fetch_all():
    """등록된 모든 소스를 크롤링해서 하나의 리스트로 합친다.

    반환: (전체 행, 오류 문구 목록, [(소스 이름, 수집 건수), ...])

    소스별 건수를 같이 돌려주는 이유: "조회는 정상인데 해당 선박이 0건"과
    "조회 자체가 실패"가 화면에서는 똑같이 '아무것도 안 보임'으로 나타나
    구분할 방법이 없다. 실패한 소스는 건수를 None으로 준다.
    """
    all_rows = []
    errors = []
    stats = []
    for name, fn in SOURCES:
        try:
            rows = with_retry(fn)
        except Exception as e:  # 한 사이트가 실패해도 나머지는 계속 진행
            errors.append(f"{name}: {e}")
            stats.append((name, None))
            continue
        all_rows.extend(rows)
        stats.append((name, len(rows)))
    return all_rows, errors, stats


if __name__ == "__main__":
    data, errors, _stats = fetch_all()
    print(f"총 {len(data)}건 수집")
    for d in data[:5]:
        print(d)
    if errors:
        print("오류:", errors)
