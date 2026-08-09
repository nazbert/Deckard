from collections.abc import Callable
from typing import Any

import globals as gl
from gi.repository import Adw

class ConfirmationDialog(Adw.MessageDialog):
    def __init__(self, title: str, body: str, confirm: str, transient_for,
                 on_cancel: Callable[[], Any] | None = None,
                 on_confirm: Callable[[], Any] | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.on_cancel: Callable[[], Any] | None = on_cancel
        self.on_confirm: Callable[[], Any] | None = on_confirm

        self.set_transient_for(transient_for)
        self.set_modal(True)
        self.set_title(title)
        self.add_response("cancel", gl.lm.get("page-manager.page-editor.delete-page-confirm.cancel"))
        self.add_response("confirm", confirm)
        self.set_default_response("cancel")
        self.set_close_response("cancel")
        self.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        self.set_body(body)

        self.connect("response", self.on_response)

    def on_response(self, dialog: Adw.MessageDialog, response: str) -> None:
        if response == "cancel" and self.on_cancel:
            self.on_cancel()
        if response == "confirm" and self.on_confirm:
             self.on_confirm()

        self.destroy()