# -*- coding: UTF-8 -*-
"""A few short sound effects (notification, sent, error) played through NVDA's audio output."""
import os

SOUND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "sounds"))
_state = {"enabled": True}


def set_enabled(value):
	_state["enabled"] = bool(value)


def play(name, force=False):
	"""Play sounds/<name>.wav if it exists. Never raises."""
	if not (force or _state["enabled"]):
		return
	try:
		import nvwave
		path = os.path.join(SOUND_DIR, name + ".wav")
		if os.path.isfile(path):
			nvwave.playWaveFile(path, asynchronous=True)
	except Exception:
		pass


def play_file(path):
	try:
		import nvwave
		if os.path.isfile(path):
			nvwave.playWaveFile(path, asynchronous=True)
	except Exception:
		pass
