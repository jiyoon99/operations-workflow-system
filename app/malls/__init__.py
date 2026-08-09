"""쇼핑몰 API 설정 레지스트리.

몰마다 필요한 인증 정보가 달라서(키 2종/HMAC/OAuth/JWT…) 여기서 몰별 필드를 정의하고,
설정 화면은 이 정의를 읽어 입력폼을 그린다. 실제 값은 settings 테이블의 'malls' 키에
{몰코드: {필드: 값}} 형태로 저장되며, 비밀 필드는 응답에서 자동 마스킹된다.

필드 정의: (키, 라벨, 종류, 도움말)
  종류 secret = 마스킹 대상(이름에 key/secret/token/password 포함되게 지어야 함)
"""
from flask import jsonify

from ..auth.perms import require
from ..settings import bp

MALLS = [
    {
        "code": "godomall",
        "name": "고도몰5",
        "vendor": "NHN커머스",
        "auth": "API 키 2종(제휴사키 + 사용자키)",
        "docs": "https://devcenter.godo.co.kr/godomall5/openapi/specDownload",
        "note": "개발자센터에서 제휴사 등록 후 쇼핑몰별 사용자키를 신청합니다(담당자 승인 필요). "
                "호출 IP를 등록해야 하며 응답은 XML입니다.",
        "fields": [
            {"key": "partner_key", "label": "제휴사 인증키 (partner_key)", "type": "secret"},
            {"key": "user_key", "label": "사용자 인증키 (key)", "type": "secret"},
            # 쇼핑몰 ID 칸은 없앴다(2026-07-29) — RMS 실운영은 제휴사키+사용자키 둘로만
            # 돌고 있고, 사용자키 자체가 쇼핑몰을 식별한다.
            {"key": "shop_url", "label": "자사몰 주소", "type": "text",
             "help": "예: https://halfbook.co.kr — 셋팅 화면에서 상품명을 누르면 이 주소의 "
                     "상품 페이지로 바로 갑니다. 비워 두면 상품명으로 검색만 합니다."},
            {"key": "sandbox", "label": "샌드박스 사용", "type": "bool",
             "help": "⚠평소에는 꺼 두세요. 체크하면 테스트 서버(sbopenhub)로 호출하는데, "
                     "테스트 서버는 운영 키를 몰라서 '존재하지 않는 인증키' 오류가 납니다"
                     "(테스트 전용 키를 따로 발급받은 경우에만 사용). 2026-07-29 실제 겪은 함정."},
        ],
    },
    {
        "code": "coupang",
        "name": "쿠팡",
        "vendor": "Coupang WING",
        "auth": "HMAC-SHA256 서명",
        "docs": "https://developers.coupang.com/",
        "note": "WING > 판매자정보 > OPEN API 키 발급에서 셀프 발급합니다. 호출 IP 등록 필요. "
                "★키 유효기간이 180일이라 만료 전에 재발급해야 합니다.",
        "fields": [
            {"key": "vendor_id", "label": "업체코드 (vendorId)", "type": "text", "help": "예: A00012345"},
            {"key": "access_key", "label": "Access Key", "type": "secret"},
            {"key": "secret_key", "label": "Secret Key", "type": "secret"},
            {"key": "wing_id", "label": "판매자 ID (X-Requested-By)", "type": "text"},
            {"key": "key_expires_at", "label": "키 만료일", "type": "date",
             "help": "발급일로부터 180일. 만료 전 알림에 사용합니다."},
            # 스마트스토어와 같은 분류 — 렌탈 주문이 HMS로 들어오면 판매 출고 사고가 난다
            # (2026-08-05 대표 지적). 두 몰이 다르게 굴면 한쪽에만 구멍이 생긴다.
            {"key": "rental_product_ids", "label": "렌탈 상품번호 (수집 제외)", "type": "text",
             "help": "쉼표로 구분해 입력하세요. 이 상품 주문은 렌탈(RMS) 몫이라 가져오지 않습니다. "
                     "상품명에 렌탈·대여·사용기간이 들어간 주문은 등록 전에도 자동 제외됩니다."},
            {"key": "sale_product_ids", "label": "판매 상품번호 (항상 수집)", "type": "text",
             "help": "쉼표로 구분. 상품명에 '렌탈'이 들어가도 판매 상품이면 여기 등록하세요 — "
                     "이 목록이 자동 제외보다 우선합니다."},
        ],
    },
    {
        "code": "smartstore",
        "name": "스마트스토어",
        "vendor": "네이버 커머스API",
        "auth": "OAuth2 + bcrypt 전자서명",
        "docs": "https://apicenter.commerce.naver.com/",
        "note": "커머스API센터에서 애플리케이션을 등록해 Client ID/Secret을 발급받습니다. "
                "호출 IP를 등록해야 하며, 발송처리 시 자동으로 CJ대한통운(CJGLS)으로 올립니다.",
        "fields": [
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "secret",
             "help": "커머스API센터에서 발급한 값 그대로 — 앞뒤에 다른 글자가 섞이면 안 됩니다."},
            # 같은 스토어에 렌탈(RMS)·판매(HMS) 상품이 함께 있다 — 렌탈 주문이 HMS로
            # 들어오면 판매 출고 사고가 나므로 상품번호로 갈라낸다(RMS 분류의 거울상).
            {"key": "rental_product_ids", "label": "렌탈 상품번호 (수집 제외)", "type": "text",
             "help": "쉼표로 구분해 입력하세요. 이 상품 주문은 렌탈(RMS) 몫이라 가져오지 않습니다. "
                     "상품명에 렌탈·대여·사용기간이 들어간 주문은 등록 전에도 자동 제외됩니다."},
            {"key": "sale_product_ids", "label": "판매 상품번호 (항상 수집)", "type": "text",
             "help": "쉼표로 구분. 상품명에 '렌탈'이 들어가도 판매 상품이면 여기 등록하세요 — "
                     "이 목록이 자동 제외보다 우선합니다."},
        ],
    },
    {
        "code": "kakao",
        "name": "카카오쇼핑",
        "vendor": "톡스토어 / 선물하기",
        "auth": "KakaoAK 3중 헤더",
        "docs": "https://shopping-developers.kakao.com/",
        "note": "승인제입니다(연동 검토 신청 → 계약). 연동대행사 앱과 판매자 앱의 키가 각각 필요하며 "
                "같은 앱 키를 양쪽에 쓰면 오류가 납니다.",
        "fields": [
            {"key": "agency_admin_key", "label": "연동대행사 ADMIN 키", "type": "secret"},
            {"key": "seller_rest_key", "label": "판매자 REST API 키", "type": "secret"},
            {"key": "channel_ids", "label": "채널 ID", "type": "text",
             "help": "101=톡스토어, 1=선물하기, 둘 다면 1,101"},
        ],
    },
    {
        "code": "toss",
        "name": "토스쇼핑",
        "vendor": "Toss Shopping",
        "auth": "OAuth2 Client Credentials",
        "docs": "https://shopping-docs.toss.im/dev",
        "note": "셀러 어드민 > 쇼핑 > 연동 관리에서 Direct API 키를 발급합니다(호출 서버 IP 등록). "
                "토스페이 입점(청약)이 끝나야 발급됩니다.",
        "fields": [
            # ★토스 발급 화면의 명칭은 Access Key / Secret Key다. 내부적으로는 이 값이
            #   OAuth 인증의 client_id/client_secret 자리에 들어간다 — 라벨을 발급 화면과
            #   똑같이 쓰지 않으면 '키 이름이 다르다'고 헷갈린다(2026-07-29 대표 확인).
            {"key": "client_id", "label": "Access Key", "type": "text",
             "help": "토스 [Direct API 키 발급]에서 받은 Access Key를 그대로 넣으세요."},
            {"key": "client_secret", "label": "Secret Key", "type": "secret",
             "help": "같은 화면의 Secret Key. 두 키가 한 쌍입니다."},
            {"key": "use_alpha", "label": "테스트(alpha) 서버 사용", "type": "bool",
             "help": "⚠평소에는 꺼 두세요 — 테스트 서버는 운영 키를 모릅니다(고도몰 샌드박스와 같은 함정)."},
        ],
    },
    {
        "code": "st11",
        "name": "11번가",
        "vendor": "SK플래닛",
        "auth": "API 키 헤더(openapikey)",
        "docs": "https://openapi.11st.co.kr/",
        "note": "오픈API센터에서 셀프 발급(약 1시간). 셀러오피스에서 호출 IP를 등록해야 하며 "
                "응답이 XML(EUC-KR)입니다.",
        "fields": [
            {"key": "api_key", "label": "OPEN API KEY (32자)", "type": "secret"},
            {"key": "seller_id", "label": "셀러 ID", "type": "text"},
        ],
    },
    {
        "code": "lotteon",
        "name": "롯데온",
        "vendor": "LotteON",
        "auth": "Bearer 인증키",
        "docs": "https://api.lotteon.com/",
        "note": "판매자센터 > OpenAPI관리에서 IP 등록 후 즉시 발급. ★인증키 유효기간 1년. "
                "주문 수집 후 반드시 '연동완료 통보'를 호출해야 합니다.",
        "fields": [
            {"key": "api_key", "label": "인증키 (Bearer)", "type": "secret"},
            {"key": "vendor_no", "label": "거래처번호", "type": "text", "help": "LD/LO로 시작"},
            {"key": "key_expires_at", "label": "키 만료일", "type": "date", "help": "발급일로부터 1년"},
        ],
    },
    {
        "code": "esm",
        "name": "ESM (G마켓·옥션)",
        "vendor": "ESM PLUS",
        "auth": "Secret Key 기반 자가서명 JWT",
        "docs": "https://etapi.gmarket.com/",
        "note": "승인제입니다(etapihelp@gmail.com로 신청). 승인 후 ESM+ [셀링툴 관리]에서 등록해야 하며, "
                "주문조회는 5초당 1회 제한입니다.",
        "fields": [
            {"key": "secret_key", "label": "Secret Key", "type": "secret"},
            {"key": "master_id", "label": "ESM+ 마스터 ID", "type": "text"},
            {"key": "gmarket_id", "label": "G마켓 판매자 ID", "type": "text"},
            {"key": "auction_id", "label": "옥션 판매자 ID", "type": "text"},
        ],
    },
    {
        "code": "temu",
        "name": "테무",
        "vendor": "Temu",
        "auth": "app_key + MD5 서명",
        "docs": "https://partner.temu.com/documentation",
        "note": "파트너 등록 후 셀러센터에서 앱을 승인하면 토큰이 발급됩니다. 발송·추적 권한은 별도로 "
                "부여한 뒤 토큰을 재발급해야 하고, 수취인 정보는 복호화 API가 따로 필요합니다.",
        "fields": [
            {"key": "app_key", "label": "App Key", "type": "text"},
            {"key": "app_secret", "label": "App Secret", "type": "secret"},
            {"key": "access_token", "label": "Access Token", "type": "secret"},
            {"key": "region", "label": "지역/게이트웨이", "type": "text",
             "help": "예: global(openapi-b-global.temu.com)"},
        ],
    },
]

# 모든 몰 공통 필드(레지스트리 정의 뒤에 붙는다)
COMMON_FIELDS = [
    # 자동수집 스위치·주기 칸은 없앴다(2026-07-29 대표 결정) — 키를 넣고 [이 몰 사용]을
    # 켰다는 것 자체가 수집하겠다는 뜻이다. 주기는 몰별 호출 한도에 맞춰 시스템이 정한다
    # (scheduler.MALL_INTERVAL_MIN). 필요하면 주문관리의 [🔄 새로고침]으로 즉시 동기화.
    {"key": "enabled", "label": "이 몰 사용", "type": "bool",
     "help": "켜면 주문을 자동으로 가져옵니다(주기는 몰 호출 한도에 맞춰 자동 조절, 중복은 걸러집니다)."},
    {"key": "push_invoice", "label": "송장번호 자동 전송", "type": "bool",
     "help": "켜면 송장 발급 후 이 몰에 송장번호를 올려 발송처리합니다. "
             "실제로 고객에게 배송 안내가 나가므로, 실주문 1건으로 확인한 뒤 켜세요."},
    {"key": "cj_courier_code", "label": "이 몰의 CJ대한통운 택배사 코드", "type": "text",
     "help": "몰마다 코드 체계가 다릅니다. 몰 관리자에서 확인해 입력하세요(비우면 몰 기본값)."},
    {"key": "memo", "label": "메모", "type": "text"},
]


# 몰별 CJ대한통운 택배사 코드 기본값.
# 우리는 CJ로만 나가지만 몰은 그걸 모른다 — 송장을 올릴 때 택배사 코드를 함께 보내야
# 고객 화면에서 배송조회가 걸린다. 코드 체계가 몰마다 달라서, 확인된 몰은 여기 적어 두고
# 대표가 아무것도 입력하지 않아도 되게 한다(어댑터가 같은 값을 기본으로 쓴다).
CJ_COURIER_DEFAULTS = {
    "coupang": "CJGLS",
    "smartstore": "CJGLS",
    "kakao": "CJGLS",
    "toss": "CJGLS",
    "esm": "10013",
}


def _courier_field_for(code):
    """몰마다 다른 안내를 붙인 '택배사 코드' 칸을 만든다."""
    base = next(f for f in COMMON_FIELDS if f["key"] == "cj_courier_code")
    known = CJ_COURIER_DEFAULTS.get(code)
    if known:
        return {**base,
                "help": f"비워 두면 자동으로 {known}(CJ대한통운)을 씁니다. 그대로 두셔도 됩니다.",
                "placeholder": known}
    return {**base,
            "help": "이 몰은 CJ대한통운 코드가 확인되지 않았습니다. "
                    "송장번호 자동 전송을 켤 때 몰 관리자에서 확인해 넣어 주세요."}


@bp.get("/mall-registry")
def mall_registry():
    """설정 화면이 몰별 입력폼을 그리기 위한 정의."""
    require("settings.manage")
    from .base import implemented_codes
    impl = set(implemented_codes())
    out = []
    for m in MALLS:
        common = [_courier_field_for(m["code"]) if f["key"] == "cj_courier_code" else f
                  for f in COMMON_FIELDS]
        out.append({**m, "fields": m["fields"] + common, "implemented": m["code"] in impl})
    return jsonify(out)


# 수집 라우트 등록 (MALLS 정의 이후에 import해야 함)
from . import collect  # noqa: E402,F401
