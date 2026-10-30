# -*- coding: UTF-8 -*-
"""Windows toast notifications that appear as "Friends Mirror", like OneDrive notifications (also kept in the notification centre)."""
import os
import subprocess
import threading
from logHandler import log

APP_ID = "FriendsMirror.App"
_ready = [False]

PS = r'''
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:FM_TITLE)
$b = [Security.SecurityElement]::Escape($env:FM_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual><audio silent='true'/></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:FM_APPID).Show($toast)
'''


def register():
	"""Register the app name so Windows shows 'Friends Mirror' as the sender."""
	if _ready[0]:
		return
	try:
		import winreg
		k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\\" + APP_ID)
		winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Friends Mirror")
		winreg.SetValueEx(k, "ShowInSettings", 0, winreg.REG_DWORD, 1)
		winreg.CloseKey(k)
	except Exception:
		log.error("Friends Mirror: could not register the notification name", exc_info=True)
	_ready[0] = True


def _run(title, body):
	try:
		env = dict(os.environ, FM_TITLE=title, FM_BODY=body, FM_APPID=APP_ID)
		subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS],
			env=env, creationflags=0x08000000, timeout=20, check=True, capture_output=True)
	except Exception:
		_fallback(title, body)


def _fallback(title, body):
	import wx

	def show():
		try:
			import wx.adv
			wx.adv.NotificationMessage(title, body, parent=None).Show(timeout=8)
		except Exception:
			log.error("Friends Mirror: notification failed", exc_info=True)
	wx.CallAfter(show)


def show(title, body):
	"""Show a notification from Friends Mirror without blocking NVDA."""
	register()
	threading.Thread(target=_run, args=(title, body), daemon=True).start()
