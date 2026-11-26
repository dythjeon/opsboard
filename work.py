# -*- coding: utf-8 -*-
"""본선작업현황: 지금 접안해 있는 모든 모선의 양적하 진행률.

터미널마다 '본선작업현황' 화면이 따로 있고 모양이 다 다르다. 여기서는 그걸 한 가지 형식으로 맞춘다.

    진행률(%) = (양하 완료 + 적하 완료) / (양하 합계 + 적하 합계)

예) 신선대 HEUNG-A HAIPHONG: 양하 478/479, 적하 213/529 -> (478+213)/(479+529) = 68.6 -> 69%
터미널이 화면에 진행률을 직접 적어 주는 곳도 같은 식이다(허치슨 81.45% = (408+154)/(429+261)).

반환 행: {terminal, berth, voyage, ship_name, line, eta, etd, status, progress,
          d_done, d_total, l_done, l_total, discharge, load}
  - d_*/l_* 는 양하/적하의 완료·합계 개수(van). 터미널이 안 주면 None.
  - discharge/load 는 "완료/합계" 글자(예전 화면·exe 와의 호환용).
우리 모선인지(ours)는 server.collect() 가 선석배정에서 추적 중인 선명과 맞춰 표시한다.
"""
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

import scraper
from scraper import HEADERS, _parse_dt, _norm_name


def _int(s):
    m = re.sub(r"[^\d]", "", str(s if s is not None else ""))
    return int(m) if m else 0


def _row(terminal, berth, voyage, ship_name, line, eta, etd, d_done, d_total, l_done, l_total,
         status="작업중", progress=None):
    known = None not in (d_done, d_total, l_done, l_total)
    total = (d_total + l_total) if known else 0
    if progress is None:
        progress = int(round((d_done + l_done) / total * 100)) if total else 0
    return {
        "terminal": terminal, "berth": str(berth or "").strip(), "voyage": (voyage or "").strip(),
        "ship_name": (ship_name or "").strip(), "line": (line or "").strip(), "eta": eta, "etd": etd,
        "status": status, "progress": max(0, min(100, int(progress))),
        "d_done": d_done, "d_total": d_total, "l_done": l_done, "l_total": l_total,
        "discharge": f"{d_done}/{d_total}" if known else "", "load": f"{l_done}/{l_total}" if known else "",
    }


# ---------------------------------------------------------------- 신선대 / 감만 (BPTC)
def bptc():
    """info.bptc.co.kr 첫 화면의 선석별 작업 카드. 양하·적하 칸은 '합계/완료/잔량' 순서다."""
    rows = []
    for w in scraper.fetch_bptc_work_status(only_tracked=False):
        d = [_int(x) for x in (w.get("discharge") or "").split("/")]
        l = [_int(x) for x in (w.get("load") or "").split("/")]
        if len(d) >= 2 and len(l) >= 2:
            rows.append(_row(w["terminal"], w["berth"], w["voyage"], w["ship_name"], w["line"], w["eta"], w["etd"],
                             d[1], d[0], l[1], l[0], progress=w.get("progress")))
        else:
            rows.append(_row(w["terminal"], w["berth"], w["voyage"], w["ship_name"], w["line"], w["eta"], w["etd"],
                             None, None, None, None, progress=w.get("progress")))
    return rows


# ---------------------------------------------------------------- 한진신항 (HJIT)
def hjit():
    """컨테이너 목록에서 QC 가 배정된 것만 세어 완료(C)/계획(P)으로 나눈다(터미널 화면과 같은 계산)."""
    rows = []
    for w in scraper.fetch_hjit_work_status(only_tracked=_TRACKED is not None):
        d = [_int(x) for x in (w.get("discharge") or "").split("/")]
        l = [_int(x) for x in (w.get("load") or "").split("/")]
        if len(d) == 2 and len(l) == 2:
            rows.append(_row(w["terminal"], w["berth"], w["voyage"], w["ship_name"], w["line"], w["eta"], w["etd"],
                             d[0], d[1], l[0], l[1], status=w.get("status") or "작업중", progress=w.get("progress")))
        else:
            rows.append(_row(w["terminal"], w["berth"], w["voyage"], w["ship_name"], w["line"], w["eta"], w["etd"],
                             None, None, None, None, status=w.get("status") or "작업중", progress=w.get("progress")))
    return rows


# ---------------------------------------------------------------- 허치슨 / 광양국제 (같은 계열 화면)
def _hktl(base, term_code, terminal, vessels, key):
    """모선별 작업현황(T03/bonsundetail.jsp). 목록 화면에는 진행률이 없어 접안 중인 모선마다 한 번씩 연다.
    '합계' 줄: 양하 Job|Fin.|Bal. | 적하 Job|Fin.|Bal."""
    now = datetime.now()
    session = requests.Session()
    session.headers.update(HEADERS)
    rows = []
    for v in vessels:
        if not (v["eta"] and v["etd"] and v["eta"] <= now <= v["etd"] + timedelta(hours=3)):
            continue
        if not _wanted(terminal, v["ship_name"], v["line"]):      # 모선마다 화면을 따로 열어야 해서 미리 거른다
            continue
        resp = session.post(f"{base}/T03/bonsundetail.jsp",
                            data=dict({"langType": "K", "mainType": "T03", "subType": "00", "optType": "T",
                                       "terminal": term_code, "bvvd": v["code"]}, **key(v)), timeout=25)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        cells = None
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S | re.I):
            c = scraper._kit_cells(tr)
            if c and c[0].replace("&nbsp;", "").strip() == "합계" and len(c) >= 7:
                cells = [_int(x) for x in c[1:7]]
                break
        if not cells:               # 아직 작업 전이거나 이미 끝나 표가 없는 모선
            if v["eta"] <= now <= v["etd"]:
                rows.append(_row(terminal, v["berth"], v["voyage"], v["ship_name"], v["line"], v["eta"], v["etd"],
                                 None, None, None, None, status="접안완료", progress=0))
            continue
        d_tot, d_fin, _d_bal, l_tot, l_fin, _l_bal = cells
        if d_tot + l_tot and d_fin + l_fin >= d_tot + l_tot and now > v["etd"]:
            continue                # 작업이 끝나고 출항 예정도 지난 모선은 뺀다
        rows.append(_row(terminal, v["berth"], v["voyage"], v["ship_name"], v["line"], v["eta"], v["etd"],
                         d_fin, d_tot, l_fin, l_tot))
    return rows


def hpnt():
    """선석배정 화면은 기본으로 오늘 입항분부터만 보여 줘서 어제 접안해 아직 작업 중인 배가 빠진다.
    그래서 이틀 전 날짜로 목록을 다시 받아 접안 중인 모선을 고른다."""
    url = "https://custom.hktl.com/jsp/T01/sunsuk.jsp"
    session = requests.Session()
    session.headers.update(HEADERS)
    d = datetime.now() - timedelta(days=2)
    vessels, page, pages = [], 1, 1
    while page <= min(pages, 8):
        resp = session.post(url, data={"year": str(d.year), "month": f"{d.month:02d}", "day": f"{d.day:02d}",
                                       "currentPage": str(page), "startPage": "1"}, timeout=20)
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")
        if page == 1:
            m = re.search(r"전체\s*조회\s*건수:\s*(\d+)", soup.get_text())
            pages = max(1, -(-int(m.group(1)) // 20)) if m else 1
        table = scraper._find_hpnt_table(soup)
        for tr in (table.find_all("tr")[1:] if table else []):
            c = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(c) < 14:
                continue
            # 접안은 실제 접안 시각(6번째 칸)이 있으면 그것을 쓴다
            vessels.append({"code": c[0], "berth": c[2], "voyage": f"{c[0]} {c[1]}".strip(), "ship_name": c[11],
                            "line": c[12], "eta": _parse_dt(c[5]) or _parse_dt(c[4]), "etd": _parse_dt(c[6])})
        page += 1
    # 상세 화면은 선석을 숫자로 받는다. 감만 쪽 선석("감만1 A/B")은 본선작업현황 표에서 4번이다.
    bnum = lambda v: {"bnum": "4" if "감만" in v["berth"] else (re.sub(r"[^0-9]", "", v["berth"]) or "1")}
    return _hktl("https://custom.hktl.com/jsp", "HBCTLIB", "허치슨", vessels, bnum)


def kit():
    if not scraper._KIT_ALL:
        scraper.fetch_kit()
    return _hktl(scraper.KIT_BASE, "KITLIB", "광양국제", scraper._KIT_ALL, lambda v: {"byear": str(v["eta"].year)})


# ---------------------------------------------------------------- 광양서부 (GWCT)
def gwct():
    """/sub/sub_B1. 모선이 가로로 나열되고 '총 작업' 줄에 모선마다 양하(잔량|완료|합계) 적하(잔량|완료|합계)."""
    if not scraper._GWCT_INFO:
        try:
            scraper.fetch_gwct()
        except Exception:
            pass
    resp = requests.get("http://www.gwct.co.kr/sub/sub_B1", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    html = resp.text
    heads = re.findall(r'vslCd="([^"]*)"\s+callYear="([^"]*)"\s+callSeq="([^"]*)"\s+vslNm="([^"]*)".*?'
                       r'yard-tit[^>]*>\s*([^<]*?)\s*<.*?percentText[^>]*>\s*([\d.]+)\s*%', html, re.S)
    total = None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        if "총 작업" in tr or "총작업" in tr:
            total = [_int(re.sub(r"<[^>]+>", "", c)) for c in re.split(r"<td[^>]*>", tr)[1:]]
            break
    rows = []
    for i, (cd, year, seq, name, berth, pct) in enumerate(heads):
        voyage = f"{cd}-{year}-{seq}"
        line, eta, etd = scraper._GWCT_INFO.get(f"{cd}-{seq}", scraper._GWCT_INFO.get(voyage, ("", None, None)))
        seg = total[i * 6:i * 6 + 6] if total and len(total) >= i * 6 + 6 else None
        if seg:
            _d_bal, d_fin, d_tot, _l_bal, l_fin, l_tot = seg
            rows.append(_row("광양서부", berth, voyage, name, line, eta, etd, d_fin, d_tot, l_fin, l_tot))
        else:
            rows.append(_row("광양서부", berth, voyage, name, line, eta, etd, None, None, None, None, progress=float(pct)))
    return rows


# ---------------------------------------------------------------- 선광신항 (SNCT)
def snct():
    """vslWorkStatus.jsp 의 선석별 카드: 선석 | 모선항차 | 선사 | 시각 4개 | 진행률 | 양하 완료/합계 | 적하 완료/합계.
    카드에 선명이 없어 선석배정(텍스트) 화면에서 모선항차 -> 선명을 찾아 붙인다."""
    base = scraper.SNCT_BASE
    resp = requests.get(f"{base}/vessel/vslWorkStatus.jsp", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    cards = re.split(r'<div class="vessel-card', resp.text)[1:]
    names = {}
    if any(c.lstrip().startswith(" working") or c.startswith(' working') for c in cards):
        try:
            today = datetime.now()
            r2 = requests.post(f"{base}/vessel/vslScheduleText.jsp",
                               data={"isSearch": "Y", "page": "1", "ROUTE": "",
                                     "strdStDate": (today - timedelta(days=3)).strftime("%Y-%m-%d"),
                                     "strdEdDate": (today + timedelta(days=1)).strftime("%Y-%m-%d")},
                               headers=HEADERS, timeout=20)
            r2.encoding = "euc-kr"
            for tr in BeautifulSoup(r2.text, "html.parser").find_all("tr"):
                tds = tr.find_all("td", recursive=False)
                if len(tds) < 11:
                    continue
                p = [list(td.stripped_strings) for td in tds]
                if p[2] and p[5]:
                    names[re.sub(r"[^A-Z0-9]", "", f"{p[2][0]}{p[3][0] if p[3] else ''}".upper())] = p[5][0]
        except Exception:
            pass
    rows = []
    for c in cards:
        if not c.startswith(" working"):
            continue
        txt = lambda cls: [re.sub(r"<[^>]+>|\s+", " ", m).strip()
                           for m in re.findall(r'class="%s[^"]*"[^>]*>(.*?)</(?:span|div)>' % cls, c, re.S)]
        berth = (txt("vessel-name") or [""])[0].replace("선석#", "").strip()
        voyage = (txt("vessel-voyage") or [""])[0]
        line = (txt("vessel-badge") or [""])[0]
        times = txt("time-val")
        vals = re.findall(r'work-metric-val">\s*(\d+)\s*<span class="metric-sep">/</span>\s*<span class="metric-total">\s*(\d+)', c)
        eta = _parse_dt(times[1]) if len(times) > 1 and times[1] else (_parse_dt(times[0]) if times else None)
        etd = _parse_dt(times[3]) if len(times) > 3 else None
        name = names.get(re.sub(r"[^A-Z0-9]", "", voyage.upper()), "")
        if len(vals) >= 2:
            rows.append(_row("선광신항", berth, voyage, name or voyage, line, eta, etd,
                             int(vals[0][0]), int(vals[0][1]), int(vals[1][0]), int(vals[1][1])))
        else:
            pct = re.search(r'work-ring-pct">\s*([\d.]+)', c)
            rows.append(_row("선광신항", berth, voyage, name or voyage, line, eta, etd, None, None, None, None,
                             progress=float(pct.group(1)) if pct else 0))
    return rows


# ---------------------------------------------------------------- PCTC (평택)
def pctc():
    """화면이 부르는 JSON(/esvc/vessel/vesselStatus/data). C_* = 완료, TT_* = 합계."""
    base = scraper.PCTC_BASE
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(f"{base}/esvc/vessel/vesselStatus", timeout=15).raise_for_status()
    resp = session.get(f"{base}/esvc/vessel/vesselStatus/data", headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20)
    resp.raise_for_status()
    rows = []
    for v in resp.json() or []:
        rows.append(_row("PCTC", v.get("BERTH_NO"), f"{v.get('VSL_CD') or ''}-{v.get('CALL_SEQ') or ''}", v.get("VSL_NM"),
                         v.get("PTNR_CODE"), _parse_dt(v.get("ATB") or v.get("ETB") or ""), _parse_dt(v.get("ETD") or ""),
                         _int(v.get("C_DISQTY")), _int(v.get("TT_DISQTY")), _int(v.get("C_LOADQTY")), _int(v.get("TT_LOADQTY")),
                         status="작업중" if v.get("ATW") else "접안완료"))
    return rows


# ---------------------------------------------------------------- GCT (군산)
def gct():
    """e-service 본선작업현황 표: 선석|선박명|선박운영사|입항일시|출항일시|양하|적하|진행률.
    양하·적하 칸의 숫자가 둘 이상이면 큰 쪽을 합계, 작은 쪽을 완료로 본다(표가 비어 있을 때 만들어 형식을 직접 보지 못했다)."""
    soup = scraper._gct_post({"m": "B", "s": "1"})
    rows = []
    for tr in soup.select("table.AA_list tr"):
        c = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if len(c) < 8:
            continue

        def pair(s):
            n = [int(x) for x in re.findall(r"\d+", s)]
            return (min(n[:2]), max(n[:2])) if len(n) >= 2 else (None, None)
        (dd, dt), (ld, lt) = pair(c[5]), pair(c[6])
        pct = re.search(r"[\d.]+", c[7])
        known = None not in (dd, dt, ld, lt)
        rows.append(_row("GCT", c[0], "", c[1], c[2], _parse_dt(c[3]), _parse_dt(c[4]),
                         dd if known else None, dt if known else None, ld if known else None, lt if known else None,
                         progress=float(pct.group(0)) if pct else None))
    return rows


# ---------------------------------------------------------------- E1CT (인천)
def _e1ct_key(voyage):
    """'PGLO- 027-2026' 과 'PGLO- 027 2634E/2635W' 를 같은 열쇠(PGLO027)로."""
    m = re.match(r"\s*([A-Z0-9]+)\s*-\s*0*(\d+)", (voyage or "").upper())
    return f"{m.group(1)}{int(m.group(2)):03d}" if m else ""


def e1ct():
    """/info/terminal/vesselWork: 모선마다 [선석·모선항차·선사·ETB·작업시작·ATB·ETD] 표와
    [작업량/완료량/잔여량 x 양하·적하·합계] 표가 한 묶음이다. 작업 중인 배가 없으면 표 자체가 없다.
    선명이 없어 선석배정(T) 화면에서 모선항차로 찾아 붙인다."""
    resp = requests.get("https://www.e1ct.co.kr/info/terminal/vesselWork", headers=HEADERS, timeout=20,
                        verify=scraper._e1ct_ca_bundle())
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    blocks = re.split(r"<th>\s*선석\s*</th>", resp.text)[1:]
    if not blocks:
        return []
    sched = {}
    try:
        for r in scraper.fetch_e1ct(only_tracked=False):
            sched.setdefault(_e1ct_key(r["voyage"]), r)
    except Exception:
        pass

    rows = []
    for b in blocks:
        def cell(label, text=b):
            m = re.search(r"<th>\s*%s\s*</th>\s*(?:<!--.*?-->\s*)*<td[^>]*>([^<]*)" % label, text, re.S)
            return m.group(1).strip() if m else ""

        def pair(label):
            m = re.search(r"<th>\s*%s\s*</th>\s*<td[^>]*>\s*([\d,]+)\s*</td>\s*<td[^>]*>\s*([\d,]+)" % label, b)
            return (_int(m.group(1)), _int(m.group(2))) if m else (None, None)

        voyage = cell("모선항차", "<th>선석</th>" + b)
        berth = re.match(r"\s*<td[^>]*>([^<]*)", b)
        berth = berth.group(1).strip().lstrip("#") if berth else ""
        (d_total, l_total), (d_done, l_done) = pair("작업량"), pair("완료량")
        if not voyage or None in (d_total, l_total, d_done, l_done):
            continue
        s = sched.get(_e1ct_key(voyage), {})
        rows.append(_row("E1CT", berth, s.get("voyage") or voyage, s.get("ship_name") or voyage, cell("선사"),
                         _parse_dt(cell("ATB") or cell("ETB")) or s.get("eta"), _parse_dt(cell("ETD")) or s.get("etd"),
                         d_done, d_total, l_done, l_total,
                         status="작업중" if cell("작업시작") else "접안완료"))
    return rows


# (이름, 함수)
WORK_SOURCES = [
    ("본선작업현황(신선대/감만)", bptc),
    ("본선작업현황(허치슨)", hpnt),
    ("본선작업현황(한진신항)", hjit),
    ("본선작업현황(선광신항)", snct),
    ("본선작업현황(E1CT)", e1ct),
    ("본선작업현황(광양서부)", gwct),
    ("본선작업현황(광양국제)", kit),
    ("본선작업현황(PCTC)", pctc),
    ("본선작업현황(GCT)", gct),
]


_TRACKED = None      # {(터미널, 정규화한 선명)}. None 이면 거르지 않고 접안 중인 모든 모선


def _range_gaps(rows):
    """작업 화면에는 있는데 그 터미널 선석 목록(걸러내기 전 전체)에는 아예 없는 배 -> 선석 목록의 조회 범위가
    좁다는 신호다. 우리 모선인지 알 수 없는 상태(항로를 모름)라, 조용히 넘기지 않고 오류 목록에 적는다."""
    out = []
    for r in rows:
        names = scraper._ALL_NAMES.get(r["terminal"])
        if names and _norm_name(r["ship_name"]) not in names:
            out.append(f"선석 목록 누락 의심: {r['terminal']} {r['berth']} {r['ship_name']} "
                       f"(작업 화면에는 있는데 선석 목록에 없음 - 간트에서 빠졌을 수 있음)")
    return out


def _wanted(terminal, ship_name, line):
    if _TRACKED is None:
        return True
    return (terminal, _norm_name(ship_name)) in _TRACKED or scraper._is_dys(line)


def fetch_all_work(tracked=None):
    """본선작업현황. tracked = 선석 탭에서 추적 중인 {(터미널, 정규화한 선명)} 을 주면 그 모선(과 선사 DYS)이
    지금 접안해 있을 때만 돌려준다(접안예정은 제외). None 이면 접안 중인 모든 모선.
    반환: (행 목록, 오류 문구 목록)"""
    global _TRACKED
    _TRACKED = tracked
    rows, errors = [], []
    try:
        for name, fn in WORK_SOURCES:
            try:
                got = scraper.with_retry(fn)
                errors += _range_gaps(got)
                rows += [r for r in got
                         if _wanted(r["terminal"], r["ship_name"], r["line"]) and (tracked is None or r["status"] != "접안예정")]
            except Exception as e:
                errors.append(f"{name}: {e}")
    finally:
        _TRACKED = None
    return rows, errors
