"""两厂内衬打样清单的批次归并。

以已签核的旧分组为底，把新批规格并入旧组或另开新组：

- 旧组编号与原成员固定，新规格只能整体并入，不能拆散或重排旧组；
- 任意同组两规格的 Jaccard 距离都不得超过原切线（含等于切线，与切树口径一致）；
- 在可行前提下让新增组数最少，同数时按规格编号稳定择一；
- 标出每个结果组内距离最远的规格对。

问题等价于带预着色的图着色（precolored graph coloring）：新规格是待着色节点，
固定旧组是预着色颜色，超线的新规格对之间连冲突边。规模较小时用确定性
DSATUR 分支定界求全局最优；所有判定只依赖冻结的两批依据与切线，纯函数可
重入、可恢复，同一依据重算得到同一份清单，不产生新的出单。
"""

from dataclasses import dataclass

from .analysis import _jaccard, validate_rows
from .spec import SPEC

# 回溯搜索的节点预算，超过即判定当前批次规模过大，避免后台长时间占用租约。
_NODE_BUDGET = 200_000
# 同等最优解的保留上限，超过则要求拆分批次，保证稳定择一可被复核。
_SOLUTION_BUDGET = 5_000
_NEW_PREFIX = "N"


@dataclass(frozen=True)
class _Spec:
    spec_id: str
    name: str
    traits: frozenset


def _key(spec_id):
    """规格编号稳定排序键，沿用折叠大小写的口径。"""
    return spec_id.casefold()


def _cut_of(params):
    field = next(item for item in SPEC["params"] if item["key"] == "cut_distance")
    cut = params.get("cut_distance", field["default"])
    if type(cut) not in (int, float) or not field["min"] <= cut <= field["max"]:
        raise ValueError("归并距离切线须为 0 至 1 的数值。")
    return cut


def _load_batch(rows, label):
    """校验一个批次并按规格编号稳定排序。"""
    records = sorted(validate_rows(rows), key=lambda row: _key(row["spec_id"]))
    specs = [
        _Spec(row["spec_id"], row["name"], frozenset(row["fit_traits"].split("|")))
        for row in records
    ]
    return specs, {spec.spec_id: spec for spec in specs}


def parse_groups(group_text):
    """解析签核分组文本：每行「组编号: 规格编号、规格编号」。"""
    if not isinstance(group_text, str) or not group_text.strip():
        raise ValueError("已签核分组为空，请填写旧组编号及其成员规格。")
    groups = []
    for line_no, raw in enumerate(group_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if ":" not in line and "：" not in line:
            raise ValueError(f"签核分组第 {line_no} 行缺少冒号分隔的组编号与成员。")
        code, _, member_text = line.replace("：", ":").partition(":")
        code = code.strip()
        members = [part.strip() for part in member_text.replace(",", "、").split("、") if part.strip()]
        if not code or not members:
            raise ValueError(f"签核分组第 {line_no} 行的组编号或成员为空。")
        groups.append((code, members))
    if not groups:
        raise ValueError("已签核分组为空，请填写旧组编号及其成员规格。")
    return groups


def validate_basis(old_rows, new_rows, group_text, params):
    """冻结并校验两批依据，返回搜索所需的全部静态输入。"""
    cut = _cut_of(params)
    old_specs, old_by_id = _load_batch(old_rows, "旧批台账")
    new_specs, new_by_id = _load_batch(new_rows, "新批台账")

    overlap = sorted(set(old_by_id) & set(new_by_id), key=_key)
    if overlap:
        raise ValueError(f"两批出现重复规格编号：{'、'.join(overlap)}，请保持两批编号互斥。")

    parsed = parse_groups(group_text)
    codes = [code for code, _ in parsed]
    if len(set(codes)) != len(codes):
        raise ValueError("签核分组的组编号重复，请固定唯一的旧组编号。")

    assigned = {}
    old_groups = []
    for code, members in parsed:
        missing = [member for member in members if member not in old_by_id]
        if missing:
            raise ValueError(f"旧组 {code} 的成员 {'、'.join(missing)} 不在旧批台账中。")
        for member in members:
            if member in assigned:
                raise ValueError(
                    f"规格 {member} 同时出现在旧组 {assigned[member]} 与 {code}，旧组成员须互斥。"
                )
            assigned[member] = code
        old_groups.append((code, sorted(members, key=_key)))

    orphans = sorted(set(old_by_id) - set(assigned), key=_key)
    if orphans:
        raise ValueError(f"旧批规格 {'、'.join(orphans)} 未纳入任何签核分组，请先固定旧组。")

    cache = {}

    def pair_distance(left, right):
        if left == right:
            return 0.0
        a, b = (left, right) if _key(left) < _key(right) else (right, left)
        if (a, b) not in cache:
            pool = {**old_by_id, **new_by_id}
            cache[(a, b)] = _jaccard(pool[a].traits, pool[b].traits)
        return cache[(a, b)]

    # 旧组必须在原切线内自洽：组内任意两规格都不得超线。
    for code, members in old_groups:
        for i in range(len(members)):
            for j in range(i):
                if pair_distance(members[j], members[i]) > cut:
                    raise ValueError(
                        f"旧组 {code} 中 {members[j]} 与 {members[i]} 的距离已超过切线 {cut}，"
                        "签核依据与切线不一致，请核对冻结依据。"
                    )
    return {
        "cut": cut,
        "old_specs": old_specs,
        "new_specs": new_specs,
        "old_by_id": old_by_id,
        "new_by_id": new_by_id,
        "old_groups": old_groups,
        "pair_distance": pair_distance,
    }


def _build_graph(basis):
    """构造新规格的可选旧组与新批内部冲突边。"""
    cut = basis["cut"]
    new_specs = basis["new_specs"]
    old_groups = basis["old_groups"]
    pair_distance = basis["pair_distance"]

    eligible = []
    for spec in new_specs:
        allowed = set()
        for color, (_, members) in enumerate(old_groups):
            if all(pair_distance(spec.spec_id, member) <= cut for member in members):
                allowed.add(color)
        eligible.append(allowed)

    conflicts = [set() for _ in new_specs]
    for i in range(len(new_specs)):
        for j in range(i):
            if pair_distance(new_specs[j].spec_id, new_specs[i].spec_id) > cut:
                conflicts[i].add(j)
                conflicts[j].add(i)
    return eligible, conflicts


def _greedy_new_groups(basis, conflicts):
    """对剩余冲突子图做确定性 DSATUR 贪心，给出另开新组数的可行上界。"""
    new_specs = basis["new_specs"]
    order = sorted(range(len(new_specs)), key=lambda i: _key(new_specs[i].spec_id))
    color_of = {}
    # 贪心只评估“另开新组”的代价：旧组不计入该色板，颜色从 0 起独立编号。
    while len(color_of) < len(order):
        candidates = [i for i in order if i not in color_of]

        def score(index):
            neighbor_colors = {color_of[n] for n in conflicts[index] if n in color_of}
            colored = sum(1 for n in conflicts[index] if n in color_of)
            return (-len(neighbor_colors), -colored, _key(new_specs[index].spec_id))

        index = min(candidates, key=score)
        used = {color_of[n] for n in conflicts[index] if n in color_of}
        color = next((c for c in range(len(used) + 1) if c not in used), len(used))
        color_of[index] = color
    return (max(color_of.values()) + 1) if color_of else 0


def _canonical(assignment, old_count, spec_keys):
    """把一个最优解规范化为按规格编号稳定的可比向量。

    旧组颜色保留固定次序；新组颜色彼此对称，按各组最小规格编号重新编号，
    再逐规格（编号折叠大小写排序）取颜色向量做字典序比较。
    """
    order = sorted(range(len(spec_keys)), key=lambda i: spec_keys[i])
    new_groups = {}
    for index, color in enumerate(assignment):
        if color >= old_count:
            new_groups.setdefault(color, []).append(index)
    remap = {
        color: old_count + rank
        for rank, color in enumerate(
            sorted(new_groups, key=lambda c: min(spec_keys[i] for i in new_groups[c]))
        )
    }
    return tuple(remap.get(assignment[i], assignment[i]) for i in order)


def solve(basis):
    """求新增组数最少、同数时按规格编号稳定的确定性分配。"""
    new_specs = basis["new_specs"]
    old_groups = basis["old_groups"]
    old_count = len(old_groups)
    eligible, conflicts = _build_graph(basis)
    n = len(new_specs)
    if n == 0:
        return _materialize(basis, [])

    spec_keys = [_key(spec.spec_id) for spec in new_specs]
    order = sorted(range(n), key=lambda i: spec_keys[i])
    color_of = [-1] * n
    group_members = [set(members) for _, members in old_groups]
    nodes = 0
    upper = _greedy_new_groups(basis, conflicts)
    best = {"new_groups": upper, "canonical": None, "assignment": None, "ties": 0}

    def choose_index():
        remaining = [i for i in order if color_of[i] < 0]

        def score(index):
            neighbor_colors = {color_of[x] for x in conflicts[index] if color_of[x] >= 0}
            colored = sum(1 for x in conflicts[index] if color_of[x] >= 0)
            return (-len(neighbor_colors), -colored, spec_keys[index])

        return min(remaining, key=score)

    def candidate_colors(index):
        blocked = {color_of[x] for x in conflicts[index] if color_of[x] >= 0}
        colors = [color for color in sorted(eligible[index]) if color not in blocked]
        # 已开出的新组：未被冲突邻居占用即可并入，新组之间无预着色差异。
        colors.extend(color for color in range(old_count, len(group_members)) if color not in blocked)
        # 再开一个全新组（新组颜色对称，只尝试这一个新颜色）。
        colors.append(len(group_members))
        return colors

    def remember(used_new):
        canonical = _canonical(tuple(color_of), old_count, spec_keys)
        if best["canonical"] is None or canonical < best["canonical"]:
            best["canonical"] = canonical
            best["assignment"] = tuple(color_of)
        best["ties"] += 1
        if best["ties"] > _SOLUTION_BUDGET:
            raise ValueError("同等最优的归并方案过多，无法稳定择一，请缩小新批规模后再提交。")

    def branch(used_new):
        nonlocal nodes
        nodes += 1
        if nodes > _NODE_BUDGET:
            raise ValueError("归并搜索规模超出回溯预算，请缩减新批规格后分批提交。")
        if all(color_of[i] >= 0 for i in order):
            if used_new <= best["new_groups"]:
                if used_new < best["new_groups"]:
                    best["new_groups"] = used_new
                    best["canonical"] = None
                    best["assignment"] = None
                    best["ties"] = 0
                remember(used_new)
            return
        if used_new > best["new_groups"]:
            return
        index = choose_index()
        for color in candidate_colors(index):
            creating = color == len(group_members)
            next_used = used_new + (1 if creating else 0)
            if next_used > best["new_groups"]:
                break
            if creating:
                group_members.append(set())
            color_of[index] = color
            group_members[color].add(new_specs[index].spec_id)
            branch(next_used)
            group_members[color].discard(new_specs[index].spec_id)
            color_of[index] = -1
            if creating:
                group_members.pop()

    branch(0)
    if best["assignment"] is None:
        raise ValueError("两批依据无可行归并方案，请核对切线与签核分组。")
    rows = _materialize(basis, best["assignment"])
    return rows, {"new_groups": best["new_groups"], "ties": best["ties"], "nodes": nodes}


def _farthest_pair(member_ids, pair_distance):
    """返回组内最远规格对（编号按稳定次序）及其距离；单规格组距离记 0。"""
    ordered = sorted(member_ids, key=_key)
    if len(ordered) == 1:
        return 0.0, ordered[0], ordered[0]
    worst = (None, None, None)
    for i in range(len(ordered)):
        for j in range(i):
            distance = pair_distance(ordered[j], ordered[i])
            if worst[0] is None or distance > worst[0] or (
                distance == worst[0] and (ordered[j], ordered[i]) < (worst[1], worst[2])
            ):
                worst = (distance, ordered[j], ordered[i])
    return worst


def _materialize(basis, assignment):
    """把最优着色还原为旧组沿用 + 新组另开的结果清单。

    旧组按签核固定次序输出；新组按各自最小规格编号重新编号（N01、N02…），
    使组编号与回溯内部的着色顺序解耦，保证同数稳定择一时编号确定。
    """
    old_groups = basis["old_groups"]
    new_specs = basis["new_specs"]
    old_by_id = basis["old_by_id"]
    new_by_id = basis["new_by_id"]
    pair_distance = basis["pair_distance"]
    old_count = len(old_groups)

    members_by_color = {color: list(member_ids) for color, (_, member_ids) in enumerate(old_groups)}
    for index, color in enumerate(assignment):
        members_by_color.setdefault(color, []).append(new_specs[index].spec_id)

    new_colors = sorted(
        (color for color in members_by_color if color >= old_count),
        key=lambda color: min(_key(spec_id) for spec_id in members_by_color[color]),
    )
    # 输出位置：先固定旧组，再按最小成员编号排列的新组。
    output_order = list(range(old_count)) + new_colors
    new_rank = {color: rank for rank, color in enumerate(new_colors, start=1)}

    all_by_id = {**old_by_id, **new_by_id}
    added_by_color = {}
    for index, color in enumerate(assignment):
        added_by_color.setdefault(color, []).append(new_specs[index].spec_id)

    rows = []
    for color in output_order:
        member_ids = members_by_color[color]
        is_new = color >= old_count
        code = (f"{_NEW_PREFIX}{new_rank[color]:02d}" if is_new else old_groups[color][0])
        ordered = sorted(member_ids, key=_key)
        distance, left, right = _farthest_pair(ordered, pair_distance)
        shared = frozenset.intersection(*(all_by_id[m].traits for m in ordered))
        rows.append({
            "group": code,
            "origin": "另开新组" if is_new else "沿用旧组",
            "size": len(ordered),
            "added": len(added_by_color.get(color, [])),
            "added_members": "、".join(sorted(added_by_color.get(color, []), key=_key)) or "—",
            "members": "、".join(ordered),
            "farthest_left": left,
            "farthest_right": right,
            "farthest_distance": distance,
            "shared": " | ".join(sorted(shared)) or "—",
        })
    return rows
