# image-gen

模型显式调用 `draw_image` 或 `edit_image` 时生成图片。两个工具均为 heavy，产物交给本体发送，不直接调用平台。`edit_image` 只接受本会话上一张生成结果，准备之后若底图已改变则拒绝执行；不读取用户上传或旧消息里的图片。每个会话保存最近五个资产引用，资产内容由本体管理。

插件配置示例（只含虚构引用，不含密钥）：

```yaml
plugins:
  image-gen:
    enabled: true
    config:
      backends:
        - type: openai
          model: chosen-image-model
          api_key_ref: image_api_test
      fallback_on_refusal: false
```

`model` 必填，没有默认模型。`api_key_ref` 必须在实例 `credentials` 中授权给 `image-gen`；相应文件位于实例 `secrets/`，内容是 UTF-8 凭据。插件每次通过 `ctx.credentials.read` 读取，不保存凭据。不要把密钥填入插件配置。

每个后端可选配置 `quality`（`low`、`medium`、`high`、`auto`）和 `size`（1–64 个字符的字符串，例如 `auto`、`1024x1024`、`1536x1024`）。生成和修改请求均原样发送已配置值；省略时不发送该字段，由平台使用默认值。插件不维护尺寸列表，所选模型是否支持具体值由服务端判断。

后端调用 OpenAI GPT 图像接口，默认地址为 `https://api.openai.com/v1`，可通过 `base_url` 指定兼容接口。生成使用 JSON，修改使用 multipart；每次一张 PNG，只接受 base64 图片，不下载响应中的 URL。使用宿主 HTTP 的两分钟期限，响应上限二十 MiB，输入及解码后的图片上限十 MiB。未进行真实 API 验证；测试全部使用假后端，模型支持范围由配置所选服务决定。

后端返回 `content_refused`、`rate_limited`、`transient`、`quota_exceeded` 或 `invalid_request`；只将安全错误码交给模型，不传服务错误正文。`fallback_on_refusal` 默认关闭，开启后仅内容拒绝尝试配置中的下一个后端，其他错误不自动重试或切换。本体不做次数限制；服务额度用尽如实返回 `quota_exceeded`。

工具通过本体现有幂等键、取消、权限和任务回应流程运行。回应反映整个任务的终态：工具错误交给模型正常说明后，任务仍可正常完成。取消已发出的 HTTP 请求不保证服务端不会计费。插件不做图片内容审核。

接口依据：https://developers.openai.com/api/reference/resources/images/methods/generate 和 https://developers.openai.com/api/reference/resources/images/methods/edit 。

## 供能力插件调用的绘图服务

`image.render` 的 `render` 方法接收 `prompt`、有序的 `references` 和 `size`，按 `draw_image` 的权限运行。尺寸可选 `1024x1024`、`1024x1536`、`1536x1024`。无参考图时走生成接口；有参考图时最多八张，按顺序作为重复的 `image[]` 文件字段上传到编辑接口。画质取后端配置。成功返回一张图片资产，普通画图工具用于“修改上一张”的历史独立保存。[OpenAI 图像生成指南](https://developers.openai.com/api/docs/guides/image-generation)
