"""Book-level HWPX policies shared by settings, preview and final output."""
from dataclasses import asdict, dataclass


# The recommended defaults use source evidence when available. Each switch is
# saved separately so two settings windows can change independent rules.
RULES = {
    'hwpx_front_pages': ('내지·서문·전언·목차를 새 쪽에서 시작', '--hwpx-no-front-pages'),
    'hwpx_source_typography': ('제목의 위치와 글자 크기·본문 간격을 원본에 맞춤', '--hwpx-no-source-typography'),
    'hwpx_entry_paragraphs': ('이 책의 항목 표지에서 문단 나누기', '--hwpx-no-entry-paragraphs'),
    'hwpx_footnote_groups': ('원본 하단의 번호 주석을 본문과 분리', '--hwpx-no-footnote-groups'),
    'hwpx_original_text': ('이 책의 원문 글자와 문장부호 유지', '--hwpx-original-text'),
    'hwpx_review_colors': ('HWPX에 검수 등급 글자색 표시', '--hwpx-no-review-colors'),
    'hwpx_review_letters': ('검수 표시를 글자색 중심으로 표시', '--hwpx-review-letters'),
}

CONVERSION_RULES = {
    'hwpx_convert_traditional': ('간체 → 번체', 'v_traditional', '--no-traditional'),
    'hwpx_convert_korean_forms': ('한국 표준 한자체', 'v_korean_forms', '--no-korean-forms'),
    'hwpx_convert_yun': ('云/雲 문맥 판별', 'v_yun', '--no-yun'),
    'hwpx_convert_punctuation': ('중문부호 수정', 'v_punctuation', '--no-punctuation'),
}
CONVERSION_LABELS = {'default': '공통 설정 사용', 'on': '이 책에서 켜기', 'off': '이 책에서 끄기'}


def conversion_enabled(app, variable, settings=None):
    if settings is None:
        settings = vars(app).get('_book_conversion_rules', {})
    choice = next((settings.get(key) for key, (_, name, _) in CONVERSION_RULES.items() if name == variable), None)
    if choice in ('on', 'off'):
        return choice == 'on'
    value = vars(app).get(variable)
    return value is None or bool(value.get())


def conversion_flags(app, settings=None):
    return [flag for _, variable, flag in CONVERSION_RULES.values() if not conversion_enabled(app, variable, settings)]


@dataclass(frozen=True)
class HwpxOutputRules:
    front_pages: bool = True
    source_typography: bool = True
    entry_paragraphs: bool = True
    footnote_groups: bool = True
    original_text: bool = False
    review_colors: bool = True
    review_letters: bool = False

    @classmethod
    def from_args(cls, args):
        return cls(**{key.removeprefix('hwpx_'): getattr(args, key, default)
                      for key, default in DEFAULTS.items()})


DEFAULTS = {f'hwpx_{name}': value for name, value in asdict(HwpxOutputRules()).items()}


def add_arguments(parser):
    for key, (label, flag) in RULES.items():
        default = DEFAULTS[key]
        parser.add_argument(flag, dest=key, action='store_false' if default else 'store_true', default=default,
                            help=f'HWPX 출력 규칙 {"끄기" if default else "켜기"} · {label}')


def cli_flags(values):
    return [flag for key, (_, flag) in RULES.items() if values.get(key, DEFAULTS[key]) != DEFAULTS[key]]
