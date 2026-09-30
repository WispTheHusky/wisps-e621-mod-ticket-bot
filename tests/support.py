"""Load the actual .pyw source and isolate all developer runs from user storage."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import os

sys.dont_write_bytecode=True
PREVIEWS=Path(__file__).resolve().parent/'_output'
PREVIEWS.mkdir(exist_ok=True)
tempfile.tempdir=str(PREVIEWS)

SOURCE=Path(__file__).resolve().parents[1]/'ticket_bot.pyw'
loader=importlib.machinery.SourceFileLoader('ticket_bot',str(SOURCE))
spec=importlib.util.spec_from_loader(loader.name,loader)
app=importlib.util.module_from_spec(spec)
sys.modules[loader.name]=app
loader.exec_module(app)
_storage=tempfile.TemporaryDirectory(prefix='ticket-bot-tests-')
app.DATA=Path(_storage.name)


def fixture_config():
    config=dict(app.DEFAULT_CONFIG,username='TestUser')
    return config
