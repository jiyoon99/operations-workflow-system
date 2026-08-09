"""수집 결과(workflow journal.jsonl) → app/purchase/spec_catalog.py 생성.

사용: python scripts/build_spec_catalog.py <journal.jsonl 경로> [추가 journal ...]
카탈로그를 다시 만들 때만 쓰는 개발용 스크립트다(운영 중 실행할 일 없음).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "purchase" / "spec_catalog.py"

# 저장장치 표기가 명확하면 SSD, 아니면 메모리 표기 규칙으로 판정한다.
SSD_HINT = re.compile(r"(NVMe|M\.?2|SATA|eMMC|HDD|UFS|PCIe|2\.5|3\.5|1TB|2TB|4TB|8TB)", re.I)
RAM_HINT = re.compile(r"^(D[345]L?\b|DDR|LPDDR|SO-?DIMM|\d+\s*GB?$)", re.I)


def load(paths):
    buckets = {"cpu": [], "gpu": [], "ram": [], "ssd": []}
    for p in paths:
        for line in Path(p).read_text("utf-8").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "result":
                continue
            res = obj.get("result") or {}
            field = (res.get("field") or "").strip()
            items = [str(x).strip() for x in (res.get("items") or []) if str(x).strip()]
            if field in buckets:
                buckets[field].extend(items)
            elif field == "ram_ssd":
                for it in items:
                    if SSD_HINT.search(it):
                        buckets["ssd"].append(it)
                    elif RAM_HINT.match(it):
                        buckets["ram"].append(it)
                    else:
                        buckets["ssd"].append(it)   # 판단 불가 시 저장장치 쪽
    return buckets


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = it.lower()
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def fmt(name, items):
    # json.dumps로 이스케이프 — 값에 따옴표가 들어간다('2.5" 256G')
    body = "\n".join("    " + json.dumps(i, ensure_ascii=False) + "," for i in items)
    return f"{name} = [\n{body}\n]\n"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    buckets = {k: dedupe(v) for k, v in load(sys.argv[1:]).items()}
    header = '''"""스펙 자동완성 카탈로그 (2010년 이후 노트북/PC 부품 표기).

표기는 TMS 실입력 형식을 따른다: "Intel Core i5-8350U", "Intel UHD Graphics 620",
"D4 8G"(DDR4 8GB), "NVMe 256G".
카탈로그에 없는 값도 자유 입력이 가능하며, 한 번 쓰면 자동으로 후보에 추가된다
(spec_options.py가 등록된 자산의 실제 값을 함께 제공).

자동 생성: scripts/build_spec_catalog.py — 직접 손대지 말고 스크립트로 갱신할 것.
"""

'''
    tail = '''
# 자유 입력이지만 값이 몇 개로 정해진 항목들
INCH = ["11", "11.6", "12", "12.5", "13", "13.3", "13.4", "13.6", "14", "14.2", "15", "15.6",
        "16", "16.2", "17", "17.3", "18", "21.5", "23.8", "24", "27", "32"]
BATTERY = ["O", "X", "우수", "양호", "보통", "불량", "없음",
           "90% 이상", "80% 이상", "70% 이상", "60% 이상", "50% 이하"]
CHARGER = ["O", "X", "정품", "호환", "없음"]

CATALOG = {
    "cpu": CPU,
    "gpu": GPU,
    "ram": RAM,
    "ssd": SSD,
    "inch": INCH,
    "battery": BATTERY,
    "charger": CHARGER,
    "location": [],   # 보관위치는 현장에서 쓰는 값이 쌓이도록 비워 둔다
    "maker": ["SAMSUNG", "LG", "LENOVO", "HP", "DELL", "ASUS", "ACER", "MSI", "APPLE",
              "MICROSOFT", "TOSHIBA", "SONY", "FUJITSU", "GIGABYTE", "RAZER", "한성컴퓨터",
              "주연테크", "TG삼보", "기타"],
    "model": [],      # 모델명은 매입하는 물건에 따라 쌓이도록 비워 둔다
}
'''
    text = header + "\n".join(
        fmt(name, buckets[key]) for name, key in
        (("CPU", "cpu"), ("GPU", "gpu"), ("RAM", "ram"), ("SSD", "ssd"))
    ) + tail
    OUT.write_text(text, encoding="utf-8")
    for k, v in buckets.items():
        print(f"{k}: {len(v)}")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
