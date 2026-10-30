# -*- coding: UTF-8 -*-
"""myChat - a private, end-to-end encrypted chat for NVDA. Developers: Rithishwaran and Mahadeesh."""
import wx
import gui
import globalPluginHandler
import scriptHandler
import addonHandler
from . import ui as chatui
from . import finder
from . import promo
from . import welcome

addonHandler.initTranslation()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = _("Friends Mirror")

	def __init__(self):
		super().__init__()
		finder.shared()
		tray = gui.mainFrame.sysTrayIcon
		self.menuItem = tray.toolsMenu.Append(wx.ID_ANY, _("Friends Mirror..."))
		tray.Bind(wx.EVT_MENU, lambda e: wx.CallAfter(chatui.controller().open), self.menuItem)
		# Sign in again quietly after NVDA has finished starting, so messages keep arriving in the background.
		wx.CallLater(3000, chatui.controller().start)
		promo.start()
		welcome.start(lambda: chatui.controller().open())

	def terminate(self):
		promo.stop()
		welcome.stop()
		try:
			finder.shared().terminate()
		except Exception:
			pass
		try:
			gui.mainFrame.sysTrayIcon.toolsMenu.Remove(self.menuItem)
		except Exception:
			pass
		try:
			chatui.controller().stop()
		except Exception:
			pass

	@scriptHandler.script(description=_("Open Friends Mirror"), gesture="kb:NVDA+alt+c")
	def script_open(self, gesture):
		wx.CallAfter(chatui.controller().open)

	@scriptHandler.script(description=_("Repeat the last Friends Mirror message"), gesture="kb:NVDA+alt+l")
	def script_repeat(self, gesture):
		wx.CallAfter(chatui.controller().repeat_last)

	@scriptHandler.script(description=_("Say how many unread Friends Mirror messages there are"), gesture="kb:NVDA+alt+u")
	def script_unread(self, gesture):
		wx.CallAfter(chatui.controller().say_unread)
