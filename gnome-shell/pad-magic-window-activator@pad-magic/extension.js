import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.gnome.Shell.Extensions.PadMagicWindowActivator';
const OBJECT_PATH = '/org/gnome/Shell/Extensions/PadMagicWindowActivator';
const INTERFACE_NAME = 'org.gnome.Shell.Extensions.PadMagicWindowActivator';
const DBUS_INTERFACE = `
<node>
  <interface name="${INTERFACE_NAME}">
    <method name="ActivateWindow">
      <arg type="s" name="title" direction="in"/>
      <arg type="b" name="success" direction="out"/>
    </method>
    <method name="GetFocusedWindowTitle">
      <arg type="s" name="title" direction="out"/>
    </method>
  </interface>
</node>`;

class PadMagicWindowActivatorService {
    ActivateWindow(title) {
        const normalizedTitle = `${title}`.trim();
        if (!normalizedTitle)
            return false;

        const window = this._findWindow(normalizedTitle);
        if (!window)
            return false;

        const timestamp = global.get_current_time();
        const workspace = window.get_workspace();

        if (window.minimized)
            window.unminimize();

        if (workspace)
            workspace.activate(timestamp);

        window.activate(timestamp);
        return global.display.focus_window === window;
    }

    GetFocusedWindowTitle() {
        return global.display.focus_window?.get_title() ?? '';
    }

    _findWindow(title) {
        const windows = global.get_window_actors()
            .map(actor => actor.meta_window)
            .filter(window => window && !window.skip_taskbar);

        return windows.find(window => window.get_title() === title) ??
            windows.find(window => window.get_title()?.trim() === title) ??
            null;
    }
}

export default class PadMagicWindowActivatorExtension extends Extension {
    enable() {
        this._service = new PadMagicWindowActivatorService();
        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_INTERFACE, this._service);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.DBus.session.own_name(
            BUS_NAME,
            Gio.BusNameOwnerFlags.REPLACE,
            null,
            null
        );
    }

    disable() {
        if (this._nameId) {
            Gio.DBus.session.unown_name(this._nameId);
            this._nameId = 0;
        }

        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }

        this._service = null;
    }
}
