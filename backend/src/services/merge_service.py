"""两厂清单归并的应用服务：冻结两批依据并产出框架通用结果。"""

from src.domain import merge as merge_domain

_GROUP_COLUMNS = (
    ("group", "结果组"), ("origin", "来源"), ("size", "规格数"), ("added", "并入新规格"),
    ("members", "全部规格编号"), ("added_members", "本批并入编号"),
    ("farthest_left", "最远规格对（左）"), ("farthest_right", "最远规格对（右）"),
    ("farthest_distance", "最远对 Jaccard 距离"), ("shared", "组内共有特征"),
)
_BASIS_COLUMNS = (
    ("batch", "依据批次"), ("dataset", "批次名称"), ("spec_count", "规格数"),
    ("checksum", "数据指纹"),
)


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def build_result(old_dataset, new_dataset, params):
    """纯计算入口：依据两个不可变批次与冻结参数产出归并结果。"""
    group_text = params.get("signed_groups") or ""
    basis = merge_domain.validate_basis(
        old_dataset.rows, new_dataset.rows, group_text, params
    )
    group_rows, meta = merge_domain.solve(basis)

    old_count = len(basis["old_groups"])
    new_group_rows = [row for row in group_rows if row["origin"] == "另开新组"]
    merged_into_old = sum(row["added"] for row in group_rows if row["origin"] == "沿用旧组")
    over_cut = [
        row for row in group_rows if row["farthest_distance"] > basis["cut"]
    ]  # 理论上为空，保留作防御性指标。

    basis_rows = [
        {"batch": "旧批（签核底账）", "dataset": old_dataset.name,
         "spec_count": old_dataset.row_count, "checksum": old_dataset.checksum},
        {"batch": "新批（并入台账）", "dataset": new_dataset.name,
         "spec_count": new_dataset.row_count, "checksum": new_dataset.checksum},
    ]
    headline = (
        f"{new_dataset.row_count} 条新批规格并入 {old_count} 个签核旧组，"
        f"另开 {meta['new_groups']} 个新组，共 {len(group_rows)} 个打样组。"
    )
    note = (
        f"距离切线 {basis['cut']}（含等于切线）；旧组编号与原成员固定，"
        f"{merged_into_old} 条新规格并入旧组，{new_dataset.row_count - merged_into_old} 条进入新组。"
    )
    if meta["ties"] > 1:
        note += f" 存在 {meta['ties']} 个同等最优方案，已按规格编号稳定择一。"

    return {
        "state": "ok" if not over_cut else "warning",
        "headline": headline,
        "note": note,
        "kind": "merge",
        "metrics": [
            {"label": "签核旧组", "value": old_count, "detail": "编号与成员固定，不拆分不重排"},
            {"label": "新增组数", "value": meta["new_groups"], "detail": "可行方案中的最少值"},
            {"label": "并入旧组规格", "value": merged_into_old, "detail": f"新批共 {new_dataset.row_count} 条"},
            {"label": "结果打样组", "value": len(group_rows), "detail": f"其中 {len(new_group_rows)} 个为另开新组"},
        ],
        "tables": [
            _table("冻结依据", _BASIS_COLUMNS, basis_rows),
            _table("归并打样清单与最远规格对", _GROUP_COLUMNS, group_rows),
        ],
        "charts": [{
            "title": "各打样组最远规格对距离", "kind": "bar",
            "series": [{"label": row["group"], "value": row["farthest_distance"]} for row in group_rows],
        }],
        "sections": [{
            "title": "归并口径与可恢复说明",
            "text": (
                "两批台账与签核分组在本次运行冻结为依据，组内任意规格对均不超过原切线；"
                "后台中断后重试沿用同一运行与同一依据，结果确定且不重复出单。"
                "原签核清单保持不变，本清单为新增交付。"
            ),
        }],
    }
