"""文件名解析管线（纯函数，无 IO、无网络）。

输入形如 ``[低俗小说].Pulp.Fiction.1994.BDRip.X264-TLF`` 的目录名/文件名，
输出结构化字段：中文名、英文名、年份、季集号、分集标题、搜索 query 序列。

设计原则：宁可少识别，不可误识别 —— 误判会导致整批刮削错片。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional

CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_RE = re.compile("[%s]" % CJK)

VIDEO_EXT = {
    "mp4", "mkv", "avi", "rmvb", "rm", "wmv", "mov",
    "flv", "ts", "m2ts", "mpg", "mpeg", "webm", "iso", "vob",
}

# 明确的"非正片"关键词（配合小体积判定）
EXTRA_WORDS = re.compile(
    r"sample|trailer|teaser|预告|花絮|特典|彩蛋|幕后|制作特辑|featurette|makingof|interview",
    re.I,
)

# 垃圾命名（时间戳、纯哈希、相机编号）
JUNK_NAME = re.compile(
    r"^(?:\d{8,14}|[0-9a-f]{16,}|VID[_-]?\d+|IMG[_-]?\d+|DSC[_-]?\d+|MVI[_-]?\d+|"
    r"GOPR\d+|screen[_-]?record.*)$",
    re.I,
)

# 站点水印：[最新电影www.5266ys.com] / [EZTVx.to] / www.dream2008.cn@兄弟连EP01
# 注意：尾部不能无条件用 [^\]\)）】]* 贪婪吞串——没有闭合括号时会把整个
# 片名一起吃掉（www.dream2008.cn@兄弟连EP01 会变成空串）。括号内才允许吞到右括号。
_DOMAIN = (r"[A-Za-z0-9\-]{2,30}\."
           r"(?:com|net|org|cn|cc|me|tv|to|biz|top|xyz|la|info|vip)\b")
RE_SITE = re.compile(
    # ① 括号包裹的水印：整段（含括号）删掉
    r"[\[\(（【]\s*(?:最新电影|高清电影|电影下载)?\s*(?:www\.)?" + _DOMAIN +
    r"[^\]\)）】]*[\]\)）】]"
    # ② 裸水印：必须带 www. 或中文前缀，避免把 La.La.Land 这类片名误伤
    r"|(?:(?:最新电影|高清电影|电影下载)\s*|www\.)" + _DOMAIN + r"@?",
    re.I,
)

# 任意括号段（年份已在更早阶段提取）
RE_ANY_BRACKET = re.compile(r"[\[\(（【][^\]\)）】]{0,80}[\]\)）】]")

# 片尾压制组：前面不是字母，且组内至少含一个字母。
# 只允许 1~2 个「点/下划线/@」分隔的短段 —— 真实压制组都是这种形状
# （YTS.BZ / FFansBD / WORLD / PlayfulDarkness / 10003@BBQDDQ）。
# 原来的 `[A-Za-z0-9@._]{2,26}` 太贪：`-The.Birth.Of.Hero` 是 4 段，
# 也会被当成组名整段吃掉，于是别名就丢了（Ying Xiong Cause - The Birth Of Hero
# 只剩前半截，搜不到原名《缘起》）。收紧后全库 562 个文件行为完全一致。
RE_RELEASE_GROUP = re.compile(
    r"(?<![A-Za-z])[-@](?=[A-Za-z0-9@._]*[A-Za-z])"
    r"(?:[A-Za-z0-9@]{2,20}(?:[._@][A-Za-z0-9@]{1,20})?)\s*$"
)
RE_RELEASE_GROUP2 = re.compile(
    r"[-.](?:YTS|YIFY|RARBG|TGx|PublicHD|WiKi|FRDS|CMCT|HDCTV|SARTRE|GalaxyRG\d*|"
    r"MiniSD|TLF|CnSCG|FFansBD)\b.*$",
    re.I,
)

# 画质 / 编码 / 音轨 / 字幕 关键词（token 级删除，两端不能贴字母数字）
_QUALITY_TOKENS = [
    r"2160p", r"1080p", r"1080i", r"720p", r"576p", r"480p", r"4k", r"8k", r"uhd",
    r"hdr10\+?", r"hdr", r"dolby\.?vision", r"dovi",
    r"blu-?ray", r"bdrip", r"brrip", r"bdremux", r"bd", r"web-?dl", r"webrip", r"web",
    r"hdtvrip", r"hdtv", r"dvdrip", r"dvdscr", r"dvd", r"remux", r"hdcam", r"hc",
    r"x264", r"x265", r"h\.?264", r"h\.?265", r"hevc", r"avc", r"av1", r"xvid", r"divx",
    r"10bit", r"8bit", r"hi10p",
    r"aac", r"ac3", r"eac3", r"dts(?:-hd)?(?:\.?ma)?", r"dd[p+]?\d(?:\.\d)?",
    r"truehd", r"atmos", r"dd[p+]?", r"atvp", r"dsnp", r"amzn", r"hbo", r"hulu", r"pcok", r"flac", r"opus", r"lpcm", r"pcm",
    r"\d\.\d", r"\dch", r"\daudio[s]?", r"multiaudio", r"multi",
    r"dubbed", r"repack", r"proper", r"limited", r"internal", r"extended", r"unrated",
    r"directors?\.?cut", r"imax", r"60fps", r"minisd", r"hq", r"bdrip",
    r"chs", r"cht", r"eng", r"chi", r"mandarin", r"cantonese", r"korean",
    r"hindi", r"thai", r"italian", r"french", r"japanese", r"german", r"spanish",
    r"国粤双语", r"国英双语", r"英国双语", r"国英台粤四语", r"中英双字", r"中英字幕",
    r"中文字幕", r"中字", r"国语", r"粤语", r"英语", r"双语", r"四语", r"多音轨",
    r"内封", r"特效", r"无水印", r"高清", r"蓝光", r"超清", r"完整版", r"未删减",
    r"国语音轨", r"简繁字幕", r"简英双字", r"中英双语", r"双字幕", r"字幕",
]
RE_QUALITY = re.compile(
    r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(_QUALITY_TOKENS), re.I
)

# 集号识别（按优先级）
EP_PATTERNS = [
    (re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})[\s._\-]*E(?:P)?(\d{1,3})(?![\d])", re.I), "se"),
    (re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})[\s._\-]{0,4}EP?(\d{1,3})(?![\d])", re.I), "se"),
    (re.compile(r"(?<![\dA-Za-z])(\d{1,2})[xX](\d{2,3})(?![\dA-Za-z])"), "se"),
    (re.compile(r"第\s*(\d{1,3})\s*[集话話期]"), "e"),
    (re.compile(r"[\(（\[]\s*(\d{1,3})\s*集\s*[\)）\]]"), "e"),
    (re.compile(r"(?<![A-Za-z0-9])EP?\s*[._\- ]?(\d{1,3})(?![\d])", re.I), "e"),
    (re.compile(r"(?<![A-Za-z0-9])Season\s*(\d{1,2})(?![\d])", re.I), "s"),
    (re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![\d])", re.I), "s"),
]
# 用于从标题中清掉残留的集号片段
RE_EP_CLEAN = re.compile(
    r"(?<![A-Za-z0-9])(?:S\d{1,2}[\s._\-]*E(?:P)?\d{1,3}|S\d{1,2}|EP?\d{1,3}|"
    r"第\s*\d{1,3}\s*[集话話期]|[\(（\[]\s*\d{1,3}\s*集\s*[\)）\]]|"
    r"Season\s*\d{1,2})(?![\d])",
    re.I,
)

# 括号不配对时的残留
RE_DANGLING = re.compile(r"[\[\(（【{]+\s*$|^\s*[\]\)）】}]+\s*")

RE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
RE_YEAR_RANGE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-~至]\s*((?:19|20)\d{2})(?!\d)")

# 形如 007系列15. 的前缀（本库真实存在）
RE_SERIES_PREFIX = re.compile(r"^\s*\d{1,4}\s*系列\s*\d{0,3}\s*[\.\-_·:：]?\s*")

RE_SEP_RUN = re.compile(r"[\s._\-]{2,}")
RE_TRAILING = re.compile(r"^[\s._\-·:：,，、]+|[\s._\-·:：,，、]+$")

MAX_YEAR_AHEAD = 3


RE_FOLDER_SIZE = re.compile(r"[\.\s]*大小\s*[\d.]+\s*[KMGT]B\b", re.I)


def _norm(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw)
    s = s.replace("\u3000", " ").replace("_", ".").replace("\u4e28", "")
    # 「圆桌派1-7季.大小189GB」这类标注了文件夹大小的目录名，去掉尺寸部分
    s = RE_FOLDER_SIZE.sub(" ", s)
    return s.strip()


def _strip_site(s: str) -> str:
    return RE_SITE.sub(" ", s)


def _detect_year(s: str) -> Optional[int]:
    from datetime import datetime

    limit = datetime.now().year + MAX_YEAR_AHEAD
    m = RE_YEAR_RANGE.search(s)
    if m:
        y = int(m.group(1))
        if 1900 <= y <= limit:
            return y
    cands = [(mm.start(), int(mm.group(1))) for mm in RE_YEAR.finditer(s)]
    cands = [(p, y) for p, y in cands if 1900 <= y <= limit]
    if not cands:
        return None
    # 片名本身是数字时（1917 / 2012 / 2001 A Space Odyssey），行首那个数字
    # 不是年份，应取后面真正的上映年份
    if len(cands) > 1 and cands[0][0] == 0:
        return cands[-1][1]
    return cands[0][1]


def detect_year(text: str) -> Optional[int]:
    """对外暴露：从任意文本（文件名 / 目录名 / 相对路径）里取年份。

    路径里通常目录名和文件名都带年份，取第一个满足条件的即可。
    """
    return _detect_year(_norm(text or ""))


def _ep_title_from_tail(tail: str) -> Optional[str]:
    """从 SxxExx 之后的残余里提取分集标题（遇到第一个技术标签就截断）。"""
    if not tail:
        return None
    tail = _strip_brackets(tail)
    m = RE_QUALITY.search(tail)
    if m:
        tail = tail[: m.start()]
    tail = RE_RELEASE_GROUP2.sub(" ", tail)
    tail = re.sub(r"[._]+", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip(" -")
    if not (2 <= len(tail) <= 60):
        return None
    if not re.search(r"[A-Za-z%s]" % CJK, tail):
        return None
    return tail


def _detect_episode(s: str):
    """返回 (season, episode, ep_title, match_start, match_end, kind)。"""
    for rx, kind in EP_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        if kind == "se":
            season, ep = int(m.group(1)), int(m.group(2))
        elif kind == "e":
            season, ep = None, int(m.group(1))
        else:  # 仅季号
            season, ep = int(m.group(1)), None
        if ep is not None and (ep <= 0 or ep > 400):
            continue
        if season is not None and (season <= 0 or season > 60):
            continue
        ep_title = _ep_title_from_tail(s[m.end():]) if kind == "se" else None
        return season, ep, ep_title, m.start(), m.end(), kind
    return None, None, None, -1, -1, None


_RE_HEX = re.compile(r"^[0-9A-Fa-f]{6,}$")


def _best_bracket_title(cands) -> str:
    """所有括号都被剥掉后，挑一个最像片名的括号内容。

    例：[MY-RAW][The Wizard of Oz][01][DVDRip][714x480][AVC AC3x2][B30459C2]
        -> The Wizard of Oz
    """
    best = ""
    for c in cands:
        t = (c or "").strip()
        if len(t) < 2 or _RE_HEX.match(t) or t.isdigit():
            continue
        residue = RE_QUALITY.sub("", t).strip(" .-")
        if len(residue) < 2:
            continue
        if not re.search(r"[A-Za-z%s]" % CJK, residue):
            continue
        if len(t) > len(best):
            best = t
    return best


def _split_titles(s: str):
    """拆中文名 / 英文名。"""
    if not s:
        return "", ""
    if not CJK_RE.search(s):
        return "", s
    # 找第一个 ASCII 字母位置
    idx = -1
    for i, ch in enumerate(s):
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            idx = i
            break
    if idx > 0:
        head, tail = s[:idx], s[idx:]
        head = head.strip(" .-_·:：,，、")
        if head and CJK_RE.search(head) and len(tail) >= 2:
            return head, tail.strip(" .-_")
    # 全中文（可能夹数字）
    return s.strip(" .-_·:：,，、"), ""


def _clean(s: str) -> str:
    s = re.sub(r"[._]+", " ", s)          # 发布名里的点是分隔符
    s = re.sub(r"[£￥$@|]+", " ", s)
    s = RE_DANGLING.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    return RE_TRAILING.sub("", s).strip()


def _strip_brackets(s: str) -> str:
    """循环剥离括号段，处理嵌套/不配对的情况。"""
    for _ in range(4):
        nxt = RE_ANY_BRACKET.sub(" ", s)
        if nxt == s:
            break
        s = nxt
    return s


def parse_name(raw: str, is_dir: bool = False) -> Dict:
    """解析单个目录名或文件名（不含扩展名）。"""
    raw = (raw or "").strip()
    s = _norm(raw)
    s = _strip_site(s)

    year = _detect_year(s)
    season, episode, ep_title, ep_start, ep_end, ep_kind = _detect_episode(s)

    # 领先方括号里的中文名（如 [低俗小说]、[BBC生命(01集)]）
    cn_bracket = ""
    m = re.match(r"^\s*[\[【]([^\]】]{1,60})[\]】]", s)
    if m and CJK_RE.search(m.group(1)):
        cn_bracket = _clean(RE_EP_CLEAN.sub(" ", _strip_brackets(m.group(1))))

    # 关键启发式：年份之后的内容几乎全是技术标签，直接截断
    work = s
    ym = None
    if year:
        for mm in RE_YEAR.finditer(work):
            if int(mm.group(1)) == year:
                ym = mm
                break
    if ym:
        # 截断到上映年份之后；但只删掉"被用作年份"的那一处，
        # 保留片名自带的数字（1917 / 2012 / 2001 A Space Odyssey）
        cut = work[: ym.end()]
        work = cut[: ym.start()] + " " + cut[ym.end():]
    elif ep_kind == "se" and ep_end > 0:
        work = work[: ep_start]      # SxxExx 之后是分集标题或技术标签

    if ep_kind == "se" and ep_end > 0:
        work = work.replace(s[ep_start:ep_end], " ", 1)
    work = RE_EP_CLEAN.sub(" ", work)
    # 不再统一删年份：上映年份那一处上面已单独删掉，剩下的年份只可能出现在
    # 片名里（1917 / 2012 / 2001 A Space Odyssey），不能误伤
    work = _strip_brackets(work).strip()
    work = RE_RELEASE_GROUP2.sub(" ", work)
    work = RE_RELEASE_GROUP.sub(" ", work)
    work = RE_QUALITY.sub(" ", work)
    work = RE_SERIES_PREFIX.sub(" ", work)
    work = _clean(work)
    if not work:
        work = _clean(_best_bracket_title(
            re.findall(r"[\[【]([^\]】]{1,80})[\]】]", s)))

    cn, en = _split_titles(work)
    cn = _clean(RE_EP_CLEAN.sub(" ", cn))
    en = _clean(RE_EP_CLEAN.sub(" ", en))
    if cn_bracket:
        if not cn or len(cn_bracket) <= len(cn) + 6:
            cn = cn_bracket
    cn = RE_SERIES_PREFIX.sub("", cn).strip()

    title = cn or en or _clean(s) or raw
    return {
        "raw": raw,
        "title": title,
        "cn_title": cn,
        "en_title": en,
        "year": year,
        "season": season,
        "episode": episode,
        "ep_title": ep_title,
        "is_dir": is_dir,
        "queries": build_queries(cn, en, year),
    }


# 片名尾部的"修饰语"：语言/方言、画质、版本、季号。
# 搜不到时逐层剥掉再试，例如「给阿嬷的情书 潮汕话」-> 「给阿嬷的情书」
_RE_NOISE_TAIL = re.compile(
    r"[\s·\-—_/,，、]*("
    r"潮汕话|潮州话|客家话|闽南语|台语|四川话|四川方言|东北话|上海话|"
    r"[国粤英日韩法德俄]{1,3}[双三多]语|"
    r"[国粤英日韩法德俄]{1,4}[语](?:[国粤英日韩法德俄]{1,2}语)?|"
    r"(?:国粤英日韩|中英|中日|中韩)?[双三多]语|"
    r"中英双字|中英字幕|中文字幕|中字|双字|字幕|原声|"
    r"高清|超清|蓝光|未删减|完整版|导演剪辑版|加长版|修复版|重制版|重制|"
    r"导演剪辑|加长|未分级|"
    r"(?:director'?s?|extended|final|theatrical|unrated|uncut)\s*cut|"
    r"extended\s*edition|special\s*edition|remastered|unrated|uncut|imax|"
    r"1280|1920|720p|1080p|2160p|4k|8k|hdr|"
    r"第[一二三四五六七八九十百\d]{1,3}[季部]|season\s*\d{1,2}"
    r")\s*$",
    re.I,
)
_RE_TRAIL_BRACKET = re.compile(r"[\s·\-—_]*[\(（\[【][^\)）\]】]{0,40}[\)）\]】]\s*$")
_RE_SUBTITLE_SEP = re.compile(r"\s*(?:——|--|：|:)\s*")
# 发布名里并排的「内部名 - 正式名」：Ying.Xiong.Cause.-.The.Birth.Of.Hero
# 只拆「空白相邻」的连字符：Spider-Man / X-Men 的 '-' 前后都贴字母，是词的一部分，
# 拆了就会产出「Man Homecoming」这种垃圾查询词。
_RE_ALIAS_SEP = re.compile(r"\s+[-–—]+\s*|[-–—]+\s+")


def _phrase_ok(seg: str) -> bool:
    """判断拆出来的片段是否「像一句片名」，避免把 Spider-Man 拆成 Spider。"""
    words = [w for w in seg.split() if w]
    return len(words) >= 2 or len(seg) >= 10


def _title_variants(t: str) -> List[str]:
    """由粗到细生成同一个片名的搜索变体（保持顺序、去重）。"""
    out: List[str] = []

    def push(x: str) -> None:
        x = (x or "").strip(" .-_·:：,，、")
        if len(x) >= 2 and x not in out:
            out.append(x)

    push(t)
    cur = t
    # ① 反复剥尾部修饰语（可能叠加，如「XX 国语 高清」）
    for _ in range(4):
        nxt = _RE_NOISE_TAIL.sub("", cur).strip()
        if nxt == cur or len(nxt) < 2:
            break
        cur = nxt
        push(cur)
    # ② 剥尾部括号
    nxt = _RE_TRAIL_BRACKET.sub("", cur).strip()
    if nxt != cur and len(nxt) >= 2:
        push(nxt)
        cur = nxt
    # ③ 去掉副标题（「007：大战皇家赌场」这类要保留前半段再试一次）
    parts = _RE_SUBTITLE_SEP.split(cur, maxsplit=1)
    if len(parts) == 2:
        head, tail = parts[0].strip(), parts[1].strip()
        if len(head) >= 2:
            push(head)
        if len(tail) >= 2 and len(tail) <= 40:
            push(tail)
    # ④ 纯中文标题里用空格并列的多个别名（「无痛凯恩 无痛先生 无痛侠」），
    #    逐个单独试；英文标题不能这么拆，会拆成单词
    if CJK_RE.search(cur) and not re.search(r"[A-Za-z]{3,}", cur):
        chunks = [c for c in re.split(r"\s+", cur) if len(c) >= 3]
        if len(chunks) >= 2:
            for c in chunks:
                push(c)
    # ⑤ 「内部名 - 正式名」并排：发布组常把两个名字用连字符排在一起
    #    （Ying Xiong Cause - The Birth Of Hero →
    #      《缘起》的原名正是 The Birth Of Hero）。
    #    不能见 '-' 就拆：Spider-Man / X-Men 会被拆坏，
    #    所以只保留「像一句片名」的片段（多词或足够长）。
    if re.search(r"[A-Za-z]{3,}", cur):
        segs = [s for s in (x.strip(" .-_·:：,，、")
                            for x in _RE_ALIAS_SEP.split(cur))
                if len(s) >= 4 and re.search(r"[A-Za-z]{2,}", s)]
        if len(segs) >= 2:
            for s in segs:
                if _phrase_ok(s):
                    push(s)
    return out


def build_queries(cn: str, en: str, year: Optional[int]) -> List[str]:
    """生成搜索词：中文/英文各自展开变体，先精确后宽松。"""
    out: List[str] = []
    for base in (cn, en):
        if not base:
            continue
        for v in _title_variants(base):
            for q in ((v + " %d" % year) if year else None, v):
                if q and q not in out:
                    out.append(q)
    return out[:8]


def series_slug(dir_name: str) -> str:
    """剧集归组用的稳定 slug：取目录名清洗后的标题。"""
    p = parse_name(dir_name, is_dir=True)
    return (p["cn_title"] or p["en_title"] or p["title"]).strip()


def is_junk_name(name: str) -> bool:
    return bool(JUNK_NAME.match((name or "").strip()))


def is_extra_name(name: str) -> bool:
    return bool(EXTRA_WORDS.search(name or ""))
