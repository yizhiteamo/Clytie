"""Regular model extension; the core owns the tool loop."""

from .model import OpenAIModel


class Plugin:
    async def start(self, ctx):
        self.model = OpenAIModel(ctx)

    def create_model(self):
        return self.model

    async def stop(self):
        self.model = None
