"""Korean standard Hanja forms (KS X 1001) for Korean-Hanja mixed documents.

Engines trained mostly on Japanese or Chinese print return the forms they know (説, 真, 絕, 緒),
while Korean books and answer files use the KS X 1001 forms (說, 眞, 絶, 緖). Every pair here maps
a character that is NOT in KS X 1001 to its counterpart that IS, so a form a Korean book can carry
is never changed (欠 and 缺, 余 and 餘 are both Korean: left alone). One character to one
character, so text offsets, grades and AI review positions are unchanged.
"""
from __future__ import annotations

_PAIRS = (
    "説說 真眞 絕絶 緒緖 脱脫 教敎 晚晩 研硏 黄黃 青靑 清淸 為爲 内內 呉吳 温溫 歩步 税稅 鋭銳 寛寬 様樣 "
    "図圖 戸戶 拠據 総總 経經 来來 帰歸 処處 気氣 争爭 尽盡 写寫 浅淺 応應 険險 発發 変變 斉齊 剣劍 覚覺 "
    "亜亞 悪惡 圧壓 囲圍 医醫 壱壹 隠隱 栄榮 営營 駅驛 塩鹽 縁緣 欧歐 殴毆 桜櫻 奥奧 仮假 価價 会會 壊壞 "
    "懐懷 絵繪 拡擴 殻殼 学學 楽樂 渇渴 勧勸 巻卷 陥陷 歓歡 関關 観觀 亀龜 偽僞 犠犧 旧舊 挙擧 虚虛 峡峽 "
    "挟挾 狭狹 郷鄕 暁曉 区區 駆驅 勲勳 薫薰 径徑 茎莖 恵惠 掲揭 渓溪 蛍螢 軽輕 継繼 鶏鷄 撃擊 県縣 倹儉 "
    "圏圈 検檢 献獻 権權 顕顯 験驗 厳嚴 広廣 効效 鉱鑛 号號 国國 黒黑 砕碎 済濟 斎齋 剤劑 雑雜 参參 桟棧 "
    "蚕蠶 惨慘 賛贊 残殘 糸絲 歯齒 児兒 辞辭 湿濕 実實 釈釋 寿壽 収收 従從 渋澁 獣獸 縦縱 粛肅 叙敍 将將 "
    "称稱 渉涉 焼燒 証證 奨奬 条條 状狀 乗乘 浄淨 剰剩 畳疊 縄繩 壌壤 嬢孃 譲讓 醸釀 触觸 嘱囑 寝寢 慎愼 "
    "粋粹 酔醉 随隨 髄髓 数數 枢樞 瀬瀨 声聲 静靜 摂攝 窃竊 専專 戦戰 潜潛 銭錢 践踐 禅禪 繊纖 双雙 壮壯 "
    "荘莊 捜搜 挿揷 巣巢 装裝 騒騷 増增 蔵藏 臓臟 即卽 属屬 続續 堕墮 対對 体體 帯帶 滞滯 滝瀧 択擇 沢澤 "
    "担擔 単單 胆膽 団團 断斷 弾彈 遅遲 虫蟲 昼晝 鋳鑄 庁廳 徴徵 聴聽 鎮鎭 逓遞 鉄鐵 転轉 伝傳 灯燈 当當 "
    "党黨 盗盜 稲稻 闘鬪 徳德 独獨 読讀 届屆 弐貳 悩惱 脳腦 拝拜 廃廢 売賣 麦麥 髪髮 抜拔 蛮蠻 瓶甁 払拂 "
    "仏佛 併倂 並竝 餅餠 辺邊 舗鋪 穂穗 宝寶 翻飜 毎每 満滿 黙默 弥彌 訳譯 薬藥 与與 誉譽 揺搖 謡謠 頼賴 "
    "乱亂 覧覽 竜龍 両兩 猟獵 緑綠 涙淚 塁壘 礼禮 励勵 戻戾 霊靈 齢齡 暦曆 歴歷 恋戀 錬鍊 炉爐 労勞 楼樓 "
    "録錄 湾灣 姫姬 頬頰"
)
KOREAN_FORMS = {pair[0]: pair[1] for pair in _PAIRS.split()}
_TABLE = str.maketrans(KOREAN_FORMS)


def korean_forms(text: str) -> str:
    return text.translate(_TABLE)


def apply_to_reading(reading) -> int:
    """Rewrite every text column of a PageReading in place; returns the characters changed."""
    changed = 0
    for region in reading.regions:
        for paragraph in region.paragraphs:
            for column in paragraph.columns:
                new = korean_forms(column.text)
                if new != column.text:
                    changed += sum(1 for a, b in zip(column.text, new) if a != b)
                    column.text = new
    return changed
