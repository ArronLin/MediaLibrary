"""目录遍历与媒体条目分组（纯函数，不碰数据库、不联网）。

分组是"增量同步幂等"的基础：同一批文件必须稳定地产生同一批 group_key。
"""
from __future__ import annotations

import fnmatch
import os
import sys
from typing import Dict, Iterable, List, Optional, Set

import re

from . import parser as P

RE_BRACKET_NUM = re.compile(r"[\[【]\s*(\d{1,3})\s*[\]】]")

# 目录名比文件标题多出来的部分若是季/部标记（第五季、Season 2、S01），
# 说明这部分是有意义的；否则（TV 01-52、圣城loveliness）是噪声
RE_SEASON_MARK = re.compile(
    r"第\s*[0-9一二三四五六七八九十]+\s*[季部]|season\s*\d|(?<![a-z])s\d{1,2}(?!\d)",
    re.I,
)

SKIP_DIRS = {
    "$recycle.bin", "system volume information", "@eadir", "subs", "sub",
    "sample", "samples", "bdmv", "certificate", "lost+found", "node_modules",
    "__macosx", "extras", "featurettes", "trailers",
}

# 这些目录名通常是"分类容器"，本身不该成为一个条目
CONTAINER_NAMES = {
    "剧集", "电影", "电视剧", "动漫", "动画", "纪录片", "综艺", "亚洲", "欧美",
    "日韩", "港台", "合集", "系列", "movie", "movies", "tv", "tvshows",
    "tv shows", "film", "films",
}

# 目录名里没有片名、只有"第几季/正片/视频"这类泛指词，需要向上取父目录名
RE_GENERIC_DIR = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百零\d]{1,3}\s*[季部]\s*)?"
    r"(?:视频|正片|全集|合集|片源|资源|未分类|新建文件夹|其他|其它)?$", re.I)
RE_SEASON_ONLY = re.compile(r"第\s*[一二三四五六七八九十百零\d]{1,3}\s*[季部]")
RE_SEASON_RANGE = re.compile(r"\s*\d{1,3}\s*[-~至]\s*\d{1,3}\s*[季部]")


def compile_excludes(patterns):
    """把用户填写的排除规则归一化（大小写不敏感，统一用反斜杠）。"""
    out = []
    for p in patterns or []:
        q = str(p).strip().replace("/", "\\").lower()
        if q:
            out.append(q)
    return out


def is_excluded(rel_path: str, patterns) -> bool:
    r"""判断相对路径是否命中排除规则。

    支持四种写法（大小写不敏感）：
      剧集\预告           —— 以目录路径开头，整个子树都排除
      *.ts               —— 通配，匹配任意层级
      第*集               —— 通配，匹配任意一层的目录名/文件名
      花絮                —— 纯文字（无分隔符无通配），路径中包含该串即排除
    """
    if not patterns:
        return False
    rp = str(rel_path).replace("/", "\\").lower()
    segs = rp.split("\\")
    for pat in patterns:
        if fnmatch.fnmatch(rp, pat):
            return True
        if "\\" not in pat:
            if any(fnmatch.fnmatch(seg, pat) for seg in segs):
                return True
            if "*" not in pat and "?" not in pat and pat in rp:
                return True
        elif rp.startswith(pat.rstrip("\\") + "\\"):
            return True
    return False


def norm_rel(rel_path: str) -> str:
    return rel_path.replace("/", "\\").lower()


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


def _stem(name: str) -> str:
    i = name.rfind(".")
    return name[:i] if i > 0 else name


def walk(root: str, video_ext: Set[str], min_size_mb: float = 20,
         max_depth: int = 6, ignored_keys: Iterable[str] = (),
         on_progress=None, exclude=()) -> List[Dict]:
    """递归收集视频文件。返回按 rel_path 排序的列表。"""
    root = os.path.abspath(root)
    ignored = set(ignored_keys)
    excl = compile_excludes(exclude)
    min_bytes = int(min_size_mb * 1024 * 1024)
    out: List[Dict] = []

    def rec(path: str, depth: int) -> None:
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        continue
                    low = e.name.lower()
                    if low in SKIP_DIRS or low.startswith("."):
                        continue
                    if excl and is_excluded(os.path.relpath(e.path, root), excl):
                        continue
                    rec(e.path, depth + 1)
                elif e.is_file(follow_symlinks=False):
                    ext = _ext(e.name)
                    if ext not in video_ext:
                        continue
                    rel = os.path.relpath(e.path, root)
                    if excl and is_excluded(rel, excl):
                        continue
                    key = norm_rel(rel)
                    if key in ignored:
                        continue
                    st = e.stat()
                    if st.st_size <= 0:
                        continue
                    if st.st_size < min_bytes and P.is_extra_name(e.name):
                        continue
                    out.append({
                        "rel_path": rel,
                        "rel_path_key": key,
                        "name": e.name,
                        "stem": _stem(e.name),
                        "size": int(st.st_size),
                        "mtime": float(st.st_mtime),
                        "ext": ext,
                        "dir": os.path.dirname(rel),
                        "depth": depth,
                    })
                    if on_progress and len(out) % 200 == 0:
                        on_progress(len(out))
            except OSError:
                continue

    rec(root, 0)
    out.sort(key=lambda f: f["rel_path"])
    return out


def _mk_file(f: Dict, parsed: Dict, season, episode, ep_title,
             role: str = "main", ord_: int = 0) -> Dict:
    return {
        "rel_path": f["rel_path"],
        "rel_path_key": f["rel_path_key"],
        "size": f["size"],
        "mtime": f["mtime"],
        "ext": f["ext"],
        "season": season,
        "episode": episode,
        "ep_title": ep_title,
        "role": role,
        "ord": ord_,
        "_parsed": parsed,
    }


def _group(files: List[Dict], group_key: str, kind: str, title: str,
           year: Optional[int], original_title: Optional[str] = None,
           dir_name: str = "") -> Dict:
    return {
        "group_key": group_key,
        "kind": kind,
        "title": title,
        "original_title": original_title,
        "year": year,
        "dir": dir_name,
        "files": files,
    }


def _multi_version_key(parsed: Dict) -> str:
    base = parsed["title"] or parsed["raw"]
    import re
    base = re.sub(r"(?i)(?:cd|part|disc|disk)\s*\d+$", "", base).strip()
    base = re.sub(r"(?i)[\s._\-]*[ab]$", "", base).strip()
    return base.lower()


def _dir_title(rel_dir: str, dir_name: str) -> str:
    """目录名清洗后的标题；根目录或分类容器返回空串。"""
    if not rel_dir or not dir_name:
        return ""
    if dir_name.strip().lower() in CONTAINER_NAMES:
        return ""
    title = (P.parse_name(dir_name, is_dir=True)["title"] or "").strip()
    # 「第七季视频」「正片」这类名字本身不含片名，往上找一层
    # （圆桌派\第七季视频 → 圆桌派 第七季）
    if title and RE_GENERIC_DIR.match(title):
        parent = os.path.basename(os.path.dirname(rel_dir)) if os.path.dirname(rel_dir) else ""
        pt = (P.parse_name(parent, is_dir=True)["title"] or "").strip() if parent else ""
        # 「圆桌派1-7季」这种范围写法去掉，只留片名
        pt = RE_SEASON_RANGE.sub("", pt).strip(" ·-—_")
        if pt and pt.lower() not in CONTAINER_NAMES:
            season = RE_SEASON_ONLY.search(title)
            return (pt + " " + season.group(0)) if season else pt
    return title


def _series_slug(rel_dir: str, dir_name: str, parsed: List[Dict]) -> str:
    """剧集归组 slug。

    目录名和文件标题各有脏的情况，规则：
      · 互不包含            -> 用目录名（文件标题常带压制组/站点前缀）
      · 目录名 ⊃ 文件标题   -> 多出来的部分是 "TV 01-52 / 1080p / 全集" 这类
                               技术噪声才用文件标题，否则保留目录名
                               （"圆桌派 第五季" 的"第五季"是有意义的）
      · 文件标题 ⊃ 目录名   -> 用目录名（文件标题常带日期/站点前缀）
    """
    from collections import Counter
    names = [p["title"] for p in parsed if p.get("episode") is not None and p["title"]]
    file_title = Counter(names).most_common(1)[0][0] if names else ""
    dir_title = _dir_title(rel_dir, dir_name)
    if not file_title:
        return dir_title or dir_name or "未命名剧集"
    if not dir_title:
        return file_title or dir_name or "未命名剧集"
    a, b = file_title.lower(), dir_title.lower()
    if a in b and a != b and not RE_SEASON_MARK.search(b.replace(a, "", 1)):
        return file_title
    return dir_title


# 裸编号集号的两种写法：
#   01 Darkness Falls              —— 编号在行首
#   神探亨特 01 - 亚利安组织        —— 编号在片名后，跟一个分隔符
RE_BARE_EP_HEAD = re.compile(r"^\s*(\d{1,3})\s*[\.\-_、:： ]\s*(\S.*)$")
RE_BARE_EP_TAIL = re.compile(r"^(.{2,40}?)\s+(\d{1,3})\s*[-–—.、:：]\s*(\S.*)$")

# 片名 + 分隔符 + 分集标题（没有集号的那种）
#   猫和老鼠四川方言版-假老练妈妈 / 猫和老鼠四川方言版－人造冰场
RE_PREFIX_SPLIT = re.compile(r"^(.{2,40}?)\s*[-－—–]\s*(\S.{0,40})$")


def _detect_prefix_episodes(vids: List[Dict]):
    """目录内所有文件都是「同一个片名 + 分隔符 + 不同小标题」，且都没有集号。

    典型场景：`剧集\\猫和老鼠四川方言版\\猫和老鼠四川方言版-假老练妈妈.rm`。
    这种目录原本会被判成 N 部独立的电影，海报墙上就会出现 N 张同系列海报。

    条件（都在目录级别判断，避免误伤）：
      ① 至少 3 个文件
      ② 每个文件都能切成「前缀 + 尾巴」，且前缀完全一致
      ③ 前缀至少 4 个字符（纯英文至少 4 个字母）
      ④ 尾巴互不相同（否则只是同一部片的不同版本）
    """
    n = len(vids)
    if n < 3:
        return {}, ""
    raw = []
    for f in vids:
        m = RE_PREFIX_SPLIT.match(f["stem"])
        raw.append((m.group(1).strip(), m.group(2).strip()) if m else None)
    matched = [i for i, p in enumerate(raw) if p]
    # 允许少量「写法不一样」的文件（猫和老鼠.四川方言版.第2辑.3.大力鼠.rm），
    # 它们会被当成花絮挂在剧集详情页，不影响主分组
    if len(matched) < 3 or len(matched) / n < 0.7:
        return {}, ""
    tally: Dict[str, int] = {}
    for i in matched:
        tally[raw[i][0]] = tally.get(raw[i][0], 0) + 1
    prefix = max(tally, key=lambda k: tally[k])
    if len(prefix) < 4:
        return {}, ""
    keep = [i for i in matched if raw[i][0] == prefix]
    if len(keep) < 3:
        return {}, ""
    tails = [raw[i][1] for i in keep]
    if len(set(tails)) < max(3, int(len(tails) * 0.8)):
        return {}, ""
    # 按尾巴排序，让集号顺序稳定
    order = sorted(keep, key=lambda i: raw[i][1])
    hits = {}
    for ep, i in enumerate(order, 1):
        hits[i] = (ep, raw[i][1])
    return hits, prefix


def _detect_bare_episodes(vids: List[Dict]):
    """目录级识别"裸编号"集号，返回 ({索引: (集号, 分集标题)}, 标题提示)。

    单看一个文件名猜集号是危险的（12 Angry Men / 127 Hours 都是片名），
    所以这里只在**整个目录**同时满足以下条件时才认定：
      ① 至少 3 个文件命中同一种编号写法
      ② 编号互不重复
      ③ 编号最大值没有离谱地超过文件数（排除 127 这种片名数字）
      ④ 命中率 >= 60%
    另外尾号写法还要求"编号前的片名完全一致"，避免把不同电影串成一部剧。
    """
    n = len(vids)
    if n < 3:
        return {}, ""

    for form in ("head", "tail"):
        hits: Dict[int, tuple] = {}
        prefix = None
        conflict = False
        for i, f in enumerate(vids):
            stem = f["stem"]
            if form == "head":
                m = RE_BARE_EP_HEAD.match(stem)
                if not m:
                    continue
                pre, num, tail = "", int(m.group(1)), m.group(2)
            else:
                m = RE_BARE_EP_TAIL.match(stem)
                if not m:
                    continue
                pre, num, tail = m.group(1).strip(), int(m.group(2)), m.group(3)
                if prefix is None:
                    prefix = pre
                elif pre != prefix:
                    conflict = True
                    continue
            if num <= 0 or num > 400:
                continue
            hits[i] = (num, tail.strip(" .-_·:：") or None)
        if conflict or len(hits) < 3:
            continue
        nums = [v[0] for v in hits.values()]
        if len(set(nums)) != len(nums):
            continue
        if max(nums) > max(len(nums) * 2, 20):
            continue
        if len(hits) / n < 0.6:
            continue
        return hits, (prefix or "")
    return {}, ""


def _movies_from_files(vids: List[Dict], parsed: List[Dict],
                       idxs: List[int], rel_dir: str,
                       allow_extra: bool = False) -> List[Dict]:
    """把一组非集号文件转成电影条目；同片多版本（CD1/CD2）合并。"""
    buckets: Dict[str, List[int]] = {}
    order: List[str] = []
    for i in idxs:
        k = _multi_version_key(parsed[i])
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(i)
    groups = []
    for k in order:
        members = buckets[k]
        p0 = parsed[members[0]]
        files = [_mk_file(vids[i], parsed[i], None, None, None,
                          role=("extra" if allow_extra and P.is_extra_name(vids[i]["name"])
                                else "main"), ord_=j)
                 for j, i in enumerate(members)]
        groups.append(_group(files, "movie:" + vids[members[0]]["rel_path"],
                             "movie", p0["title"], p0["year"],
                             p0["en_title"] or None, dir_name=rel_dir))
    return groups


def group_dir(rel_dir: str, dir_name: str, vids: List[Dict]) -> List[Dict]:
    """对"直接含视频的目录"做分组，返回条目列表。"""
    parsed = [P.parse_name(f["stem"]) for f in vids]
    n = len(vids)
    if n == 0:
        return []

    ep_idx = [i for i, p in enumerate(parsed) if p["episode"] is not None]
    junk_idx = [i for i, f in enumerate(vids) if P.is_junk_name(f["stem"])]

    # 目录级兜底：集号写成 [01] [02] 这种方括号纯数字（如 The Wizard of Oz TV 01-52）
    bn = [RE_BRACKET_NUM.search(f["stem"]) for f in vids]
    bn_nums = [int(m.group(1)) for m in bn if m]
    if (not ep_idx and len(bn_nums) >= 3 and len(set(bn_nums)) >= 3
            and len(bn_nums) / n >= 0.6):
        from collections import Counter as _C
        titles = [_C([p["title"] for p in parsed if p["title"]]).most_common(1)[0][0]] \
            if any(p["title"] for p in parsed) else []
        slug = titles[0] if titles else (_dir_title(rel_dir, dir_name) or dir_name)
        files = []
        for i, f in enumerate(vids):
            m = bn[i]
            if not m:
                continue
            p = parsed[i]
            files.append(_mk_file(f, p, 1, int(m.group(1)), p["ep_title"]))
        files.sort(key=lambda x: x["episode"] or 0)
        for j, f in enumerate(files):
            f["ord"] = j
        return [_group(files, "series:%s:%s" % (rel_dir, slug), "series", slug,
                       None, None, dir_name=rel_dir)]

    # 目录级兜底 ②：裸编号集号（01 Darkness Falls / 神探亨特 01 - 亚利安组织）
    if len(ep_idx) < 2:
        bare, bare_title = _detect_bare_episodes(vids)
        if bare:
            slug = bare_title or _dir_title(rel_dir, dir_name) or dir_name
            main_files, extra_files = [], []
            for i, f in enumerate(vids):
                if i in bare:
                    ep, ep_title = bare[i]
                    main_files.append(_mk_file(f, parsed[i], 1, ep, ep_title))
                else:
                    extra_files.append(_mk_file(f, parsed[i], None, None, None,
                                                role="extra"))
            main_files.sort(key=lambda x: x["episode"] or 0)
            for j, f in enumerate(main_files):
                f["ord"] = j
            year = (P.parse_name(dir_name, is_dir=True)["year"]
                    if _dir_title(rel_dir, dir_name) else None)
            year = year or next((p["year"] for p in parsed if p["year"]), None)
            return [_group(main_files + extra_files,
                           "series:%s:%s" % (rel_dir, slug), "series", slug,
                           year, None, dir_name=rel_dir)]

    # 目录级兜底 ③：公共前缀 + 不同小标题（猫和老鼠四川方言版-假老练妈妈 …）
    if len(ep_idx) < 2:
        pref, pref_title = _detect_prefix_episodes(vids)
        if pref:
            slug = pref_title or _dir_title(rel_dir, dir_name) or dir_name
            files = [_mk_file(vids[i], parsed[i], 1, ep, ep_title)
                     for i, (ep, ep_title) in pref.items()]
            files.sort(key=lambda x: x["episode"] or 0)
            for j, f in enumerate(files):
                f["ord"] = j
            # 写法不一致的少数文件当作花絮，挂在剧集详情页
            for i, f in enumerate(vids):
                if i not in pref:
                    files.append(_mk_file(f, parsed[i], None, None, None,
                                          role="extra"))
            return [_group(files, "series:%s:%s" % (rel_dir, slug), "series",
                           slug, None, None, dir_name=rel_dir)]

    # P1 垃圾目录（时间戳/哈希命名）：整目录合成一个 collection
    if n >= 5 and len(junk_idx) >= max(4, int(n * 0.8)):
        files = [_mk_file(f, parsed[i], None, None, None, ord_=i)
                 for i, f in enumerate(vids)]
        return [_group(files, "collection:" + rel_dir, "collection",
                       dir_name or "未命名合集", None, dir_name=rel_dir)]

    # P2 剧集目录：≥60% 文件带集号
    if len(ep_idx) >= 2 and len(ep_idx) / n >= 0.6:
        slug = _series_slug(rel_dir, dir_name, parsed)
        main_files, extra_files = [], []
        for i, f in enumerate(vids):
            p = parsed[i]
            if p["episode"] is not None:
                main_files.append(_mk_file(f, p, p["season"] or 1, p["episode"],
                                           p["ep_title"]))
            else:
                extra_files.append(_mk_file(f, p, None, None, None, role="extra"))
        main_files.sort(key=lambda x: (x["season"] or 0, x["episode"] or 0))
        for i, f in enumerate(main_files):
            f["ord"] = i
        year = (P.parse_name(dir_name, is_dir=True)["year"] if _dir_title(rel_dir, dir_name)
                else None) or next((p["year"] for p in parsed if p["year"]), None)
        return [_group(main_files + extra_files,
                       "series:%s:%s" % (rel_dir, slug), "series", slug, year,
                       None, dir_name=rel_dir)]

    # P3 单文件 = 一部电影
    if n == 1:
        f, p = vids[0], parsed[0]
        dp = P.parse_name(dir_name, is_dir=True) if _dir_title(rel_dir, dir_name) else None
        title = dp["title"] if dp else p["title"]
        year = (dp["year"] if dp and dp["year"] else p["year"])
        # 文件名里带集号的单集（Greatest.Events.of.WWII.in.Colour.E10.mkv）
        # 其实是一部剧的一集，别当成电影 —— 当电影搜不到，也拿不到海报
        if p["episode"] is not None and p["title"]:
            slug = p["title"]
            return [_group([_mk_file(f, p, p["season"] or 1, p["episode"],
                                     p["ep_title"])],
                           "series:%s:%s" % (rel_dir, slug), "series", slug,
                           year, None, dir_name=rel_dir)]
        orig = (p["en_title"] or None) if (dp and dp["cn_title"]) else None
        return [_group([_mk_file(f, p, None, None, None)],
                       "movie:" + f["rel_path"], "movie", title, year, orig,
                       dir_name=rel_dir)]

    # P4/P5：集号文件按 slug 成剧集，其余各自成电影
    groups: List[Dict] = []
    if ep_idx:
        by_slug: Dict[str, List[int]] = {}
        for i in ep_idx:
            s = (parsed[i]["title"] or _series_slug(rel_dir, dir_name, parsed)).strip()
            by_slug.setdefault(s, []).append(i)
        for slug, idxs in by_slug.items():
            if len(idxs) >= 2:
                files = []
                for i in idxs:
                    p = parsed[i]
                    files.append(_mk_file(vids[i], p, p["season"] or 1, p["episode"],
                                          p["ep_title"]))
                files.sort(key=lambda x: (x["season"] or 0, x["episode"] or 0))
                for j, f in enumerate(files):
                    f["ord"] = j
                year = next((parsed[i]["year"] for i in idxs if parsed[i]["year"]), None)
                groups.append(_group(files, "series:%s:%s" % (rel_dir, slug),
                                     "series", slug, year, None, dir_name=rel_dir))
            else:
                groups.extend(_movies_from_files(vids, parsed, idxs, rel_dir))
        ep_set = set(ep_idx)
        rest = [i for i in range(n) if i not in ep_set]
    else:
        rest = list(range(n))

    has_series = any(g["kind"] == "series" for g in groups)
    if has_series:
        # 剧集目录里的小体积/花絮文件挂到剧集详情页，不占主海报墙
        extras = [i for i in rest if P.is_extra_name(vids[i]["name"])
                  or vids[i]["size"] < 300 * 1024 * 1024]
        others = [i for i in rest if i not in set(extras)]
        if extras:
            sg = next(g for g in groups if g["kind"] == "series")
            for i in extras:
                sg["files"].append(_mk_file(vids[i], parsed[i], None, None, None,
                                            role="extra"))
        groups.extend(_movies_from_files(vids, parsed, others, rel_dir))
    else:
        groups.extend(_movies_from_files(vids, parsed, rest, rel_dir))
    return groups


def build_groups(root: str, files: List[Dict]) -> List[Dict]:
    """把 walk() 的结果按目录分组为媒体条目。"""
    root_abs = os.path.abspath(root).rstrip("\\/")
    root_name = os.path.basename(root_abs) or root_abs.rstrip("\\/").split("\\")[-1]

    by_dir: Dict[str, List[Dict]] = {}
    for f in files:
        by_dir.setdefault(f["dir"], []).append(f)

    groups: List[Dict] = []
    for rel_dir in sorted(by_dir):
        dir_name = os.path.basename(rel_dir) if rel_dir else root_name
        try:
            groups.extend(group_dir(rel_dir, dir_name, by_dir[rel_dir]))
        except Exception as exc:  # 单个目录失败不能拖垮整次扫描
            print("[scanner] group_dir failed %s: %s" % (rel_dir, exc),
                  file=sys.stderr)
    return groups


def scan(root: str, cfg_scan: Optional[Dict] = None,
         ignored_keys: Iterable[str] = (), on_progress=None, exclude=None):
    cfg_scan = cfg_scan or {}
    ext = {e.lower() for e in cfg_scan.get("video_ext") or P.VIDEO_EXT}
    excl = exclude if exclude is not None else (cfg_scan.get("exclude") or [])
    files = walk(root, ext, cfg_scan.get("min_size_mb", 20),
                 cfg_scan.get("max_depth", 6), ignored_keys, on_progress, excl)
    return files, build_groups(root, files)


def _main(argv: List[str]) -> int:
    import argparse
    import json
    from collections import Counter

    ap = argparse.ArgumentParser(description="扫描分组 dry-run（不写库、不联网）")
    ap.add_argument("--root", required=True)
    ap.add_argument("--limit", type=int, default=0, help="只打印前 N 个条目")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--filter", default="", help="只看 group_key 含该串的条目")
    ap.add_argument("--exclude", default="", help="排除规则，逗号分隔，如 '花絮,*.ts'")
    args = ap.parse_args(argv)

    excl = [x for x in args.exclude.split(",") if x.strip()]
    files, groups = scan(args.root, {"exclude": excl})
    if excl:
        print("排除规则:", excl)
    print("视频文件: %d   条目: %d" % (len(files), len(groups)))
    print("类型分布:", dict(Counter(g["kind"] for g in groups)))

    rows = groups
    if args.filter:
        rows = [g for g in rows if args.filter.lower() in g["group_key"].lower()]
    if args.limit:
        rows = rows[: args.limit]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print("%-3s %-9s %-34s %-6s %-4s %s" %
          ("#", "kind", "title", "year", "集", "group_key"))
    for i, g in enumerate(rows, 1):
        eps = sum(1 for f in g["files"] if f["episode"] is not None)
        print("%-3d %-9s %-34s %-6s %-4s %s" % (
            i, g["kind"], (g["title"] or "")[:34], g["year"] or "-",
            eps or "-", g["group_key"][:80]))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
