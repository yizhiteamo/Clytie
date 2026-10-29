"""显式公开成员的只读外观；不向插件交出宿主或后端对象。

这是 SDK 能力接口的封装，不是对任意 Python 反射或文件访问的沙箱。
"""

import inspect


def surface(target, names):
    values = {}
    for name in names:
        member = getattr(target, name)
        if inspect.iscoroutinefunction(member):

            def wrap_async(function):
                async def invoke(*args, **kwargs):
                    return await function(*args, **kwargs)

                return invoke

            values[name] = wrap_async(member)
        elif callable(member):

            def wrap(function):
                def invoke(*args, **kwargs):
                    return function(*args, **kwargs)

                return invoke

            values[name] = wrap(member)
        else:
            values[name] = member

    class Surface:
        __slots__ = ()

        def __getattribute__(self, name):
            if name not in values:
                raise AttributeError(name)
            return values[name]

        def __setattr__(self, name, value):
            raise AttributeError("SDK surface is read-only")

        def __dir__(self):
            return sorted(values)

    return Surface()
