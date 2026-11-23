"""Book-level HWPX policies shared by settings, preview and final output."""
from dataclasses import asdict, dataclass


# The recommended defaults use source evidence when available. Each switch is
# saved separately so two settings windows can change independent rules.
RULES = {
    'hwpx_front_pages': ('내지·서문·전언·목차를 새 쪽에서 시작', '--hwpx-no-front-pages'),
    'hwpx_source_typography': ('제목의 위치와 글자 크기·본문 간격을 원본에 맞춤', '--hwpx-no-source-typography'),
    'hwpx_entry_paragraphs': ('[氣味]·[主治] 등 항목을 새 문단으로 분리', '--hwpx-no-entry-paragraphs'),
    'hwpx_footnote_groups': ('원본 하단의 번호 주석을 본문과 분리', '--hwpx-no-footnote-groups'),
}


@dataclass(frozen=True)
class HwpxOutputRules:
    front_pages: bool = True
    source_typography: bool = True
    entry_paragraphs: bool = True
    footnote_groups: bool = True

    @classmethod
    def from_args(cls, args):
        return cls(**{key.removeprefix('hwpx_'): getattr(args, key, default)
                      for key, default in DEFAULTS.items()})


DEFAULTS = {f'hwpx_{name}': value for name, value in asdict(HwpxOutputRules()).items()}


def add_arguments(parser):
    for key, (label, flag) in RULES.items():
        parser.add_argument(flag, dest=key, action='store_false', default=DEFAULTS[key],
                            help=f'HWPX 출력 규칙 끄기 · {label}')


def cli_flags(values):
    return [flag for key, (_, flag) in RULES.items() if values.get(key) is False]
