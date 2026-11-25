# comic-maker 漫画工具

启用本插件和 `image-gen` 后，模型可以用 `draw_comic` 把分镜画成多页漫画。每页调用一次 `image.render`，尺寸为 1024×1536，画质沿用画图后端配置。所有页都成功才会按顺序交付。各页的角色设定和参考图会随请求发给画图服务商。

## 角色与参考图

`self` 表示本伴侣当前的形象。其他角色由主人登记。下面的命令以虚构角色为例，文件路径指向运行 CLI 的机器。

```sh
clytie plugin comic-maker character-import '{"companion_id":"companion_a","character_id":"sprout","name":"嫩芽","description":"蓝色花冠，绿色围裙","keywords":["花店"],"file":"sprout.png"}'
clytie plugin comic-maker character-import '{"companion_id":"companion_a","character_id":"sprout","file":"sprout-side.png"}'
clytie plugin comic-maker character-set '{"companion_id":"companion_a","character_id":"sprout","conversations":["test_group"],"attribution":"虚构示例"}'
```

第一条命令会展示图像尺寸和检查结果，再由本机 API 请求确认。请勿导入真人照片。动画会完整解码，只保留第一帧。导入时去掉元数据，长边最多保留 2048 像素。每个角色最多六张图，第一张是主参考图。

角色可只登记文字，在 `character-set` 中填写 ID、名字和外观描述即可。ID 只用小写字母、数字和连字符，`self` 为保留名。尚未设置 `conversations` 的角色可以出现在任何会话中。修改已有角色时，仅更新命令中给出的字段。

## 字体和页眉页脚

```sh
clytie plugin comic-maker font-import '{"companion_id":"companion_a","purpose":"title","file":"comic-title.otf"}'
clytie plugin comic-maker font-import '{"companion_id":"companion_a","purpose":"caption","file":"comic-caption.ttf"}'
```

上传单个 TTF 或 OTF（最多 30 MiB）。预览会显示字体名称、用途和常用简体中文字符检查结果。请核对所用版本的许可证，确认允许这样使用后再提交。插件保存在本机确认许可证的时间。`sfx` 用途可提前导入，留给以后的排版。

可按用途查找以下字体，获取时核对该版本随附的 SIL OFL 许可证。

| 用途 | 可选字体 |
| --- | --- |
| 标题和说明 | 思源黑体（Noto Sans CJK SC） |
| 圆润的对话风格 | 资源圆体 |
| 手写风格的旁白 | 霞鹜文楷 |
| 醒目的标题 | 站酷快乐体、站酷庆科黄油体、得意黑 |

同时导入标题和说明字体后，每页上方排标题和页码，下方排本地日期及 `footer_text`。程序保留整张漫画的画面。任一用途还没有字体时，直接使用生成的原页。把 `header_footer` 设为 `false` 可以关闭排版。

## 分镜与权限

对方指定了页数或格数时按要求画。没说页数时按内容决定，通常 1–4 页，每页一般 6–9 格，短笑话可以少几格。例如“画两页的小漫画，讲花店从开门到打烊的故事”，可以第一页画开门迎客，第二页画打烊前的趣事。

每页的 `panels` 至少有一格。每格用 `scene` 写清场景、动作和表情，用 `characters` 列出角色 ID，用 `dialogue` 列出每句的 `speaker` 和 `text`，可加拟声词 `sfx`。没有对白时传空列表。台词要短，准确的数字、产品名和来源放在配文中。真人用文字提到或画不露脸的剪影。可在工具参数中传 `style` 覆盖插件的默认画风。

先画第 1 页。后续页若延续上一页的场景，写 `"continues": true`，插件会等上一页画好并用它作参考。换场景、换话题的页可省略这个字段，它们以第 1 页为画风参考，可以同时画。第一页会忽略这个字段。从第 2 页起，每页的提示词都会带上上一页最后一格的场景和台词，用于衔接。

| 插件配置 | 默认值 | 用途 |
| --- | --- | --- |
| `max_pages` | 10 | 单次最大页数 |
| `max_parallel_pages` | 2 | 同时画 1–4 页；设为 1 时按页码逐页画 |
| `max_panels` | 10 | 每页最多格数 |
| `max_dialogue_chars` | 30 | 每句最多字数 |
| `style` | 浅色 Q 版二次元 | 整页画风 |
| `header_footer` | `true` | 使用已导入的字体排页眉页脚 |
| `footer_text` | 空 | 日期旁的说明文字 |

默认由主人调用。向群友开放时，`permissions` 和 `tool_scopes` 需同时允许在当前会话使用 `draw_comic` 与 `draw_image`。保障模式下只有主人能用这项重任务能力。允许主动开口时画漫画，可以配置 `proactive.tools.draw_comic.daily: 2`。每次成功执行的漫画页数由分镜决定，调用画图服务会产生费用。

每次漫画任务最长 3600 秒，实例的 `limits.max_task_seconds` 可以进一步缩短这个时间。任一页失败时，插件取消其他正在画的页，整本漫画不交付。已发出的请求可能产生费用，失败或限流都不会自动重试。账号每分钟出图额度较低时，可以把 `max_parallel_pages` 设为 1。
