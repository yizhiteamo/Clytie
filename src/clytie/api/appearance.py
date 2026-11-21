"""Authenticated appearance-management request contract and confirmation text."""

from clytie.appearance import MAX_BYTES, AppearanceFiles

from .auth import APIError

ROOT = "/companions/<cid>/appearance"
ADD = ROOT + "/add"
# The JSON envelope may use up to 64 KiB beyond the base64 payload.
UPLOAD_BODY_BYTES = 4 * ((MAX_BYTES + 2) // 3) + 64 * 1024
FIELDS = {
    ROOT: set(),
    ADD: {"content", "label", "state"},
    ROOT + "/edit": {"number", "label", "state"},
    ROOT + "/remove": {"number"},
}
ERROR_MESSAGES = {
    "invalid_image": "图片损坏或格式不受支持，请选择 PNG、JPEG、WebP 或 GIF。",
    "too_large": "参考图文件超过 20 MiB。",
    "too_many_pixels": "参考图超过 5000 万像素。",
    "appearance_limit": "最多可保存 6 张参考图。",
    "duplicate_appearance": "该参考图已存在。",
    "current_appearance_required": "有参考图时至少需要保留一张当前形象。",
    "invalid_appearance_label": "标签需要 1 到 20 个字。",
    "invalid_appearance_state": "形象状态应为 current 或 former。",
    "invalid_reference_number": "参考图编号无效，请用 show 查看当前编号。",
    "request_too_large": "形象导入请求超过大小上限。",
}


def validate_fields(route, data):
    allowed = FIELDS[route]
    if route != ROOT:
        allowed = allowed | {"confirm", "admin_password"}
    if data.keys() - allowed:
        raise APIError("invalid_request")


def confirmation(home, cid, clock, action, data, config):
    verbs = {"add": "导入", "edit": "修改", "remove": "删除"}
    number = ""
    if action != "add":
        if type(data.get("number")) is not int or data["number"] < 1:
            raise APIError("invalid_reference_number")
        number = f"第 {data['number']} 张"
    description = (
        f"将为伴侣 {cid} {verbs[action]}{number}参考图。参考图会在比对和自检时发给模型服务商。"
    )
    if not config.get("appearance", {}).get("self_check", True):
        return description + "自检已关闭，重启后的自检预计用量为 0 token。"
    files = AppearanceFiles(home, cid, clock)
    count = len(files.manifest()["references"]) + {"add": 1, "edit": 0, "remove": -1}[action]
    attached = min(max(0, count), config.get("vision", {}).get("max_images", 30))
    if not attached:
        return description + "保存后没有参考图，自检预计用量为 0 token。"
    try:
        text_bytes = files.path("appearance.md").stat().st_size
    except FileNotFoundError:
        text_bytes = 0
    # A planning estimate only. Actual image token accounting depends on the provider.
    estimate = 256 + (text_bytes + 2) // 3 + 2000 * attached
    return description + f"重启后会自动调用一次模型检查形象资料，预计用量约 {estimate} token。"


def execute(home, cid, clock, route, data):
    files = AppearanceFiles(home, cid, clock)
    if route == ROOT:
        return files.show()
    try:
        return files.change(route.rsplit("/", 1)[1], data)
    except ValueError as exc:
        code = str(exc)
        if code in {
            "capture_busy",
            "appearance_limit",
            "duplicate_appearance",
            "current_appearance_required",
        }:
            raise APIError(code, 409) from None
        if code in ERROR_MESSAGES or code == "invalid_request":
            raise APIError(code) from None
        raise APIError("appearance_unavailable", 409) from None
