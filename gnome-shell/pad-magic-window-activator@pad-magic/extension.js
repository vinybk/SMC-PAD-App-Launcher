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
    <method name="ActivateWindowMatching">
      <arg type="s" name="criteriaJson" direction="in"/>
      <arg type="b" name="success" direction="out"/>
    </method>
    <method name="ListWindows">
      <arg type="s" name="windowsJson" direction="out"/>
    </method>
    <method name="GetFocusedWindowTitle">
      <arg type="s" name="title" direction="out"/>
    </method>
  </interface>
</node>`;

function normalizeValue(value) {
    return `${value ?? ''}`.trim().toLowerCase();
}

function readWindowString(window, getterName, propertyName = '') {
    if (!window)
        return '';

    if (getterName && typeof window[getterName] === 'function') {
        try {
            return `${window[getterName]() ?? ''}`.trim();
        } catch (_) {
            // Ignore missing Mutter APIs across GNOME versions.
        }
    }

    if (propertyName)
        return `${window[propertyName] ?? ''}`.trim();

    return '';
}

class PadMagicWindowActivatorService {
    ActivateWindow(title) {
        const normalizedTitle = `${title}`.trim();
        if (!normalizedTitle)
            return false;

        const window = this._findWindow(normalizedTitle);
        return this._activateWindow(window);
    }

    ActivateWindowMatching(criteriaJson) {
        const criteria = this._parseCriteria(criteriaJson);
        if (!criteria)
            return false;

        const window = this._findWindowMatching(criteria);
        return this._activateWindow(window);
    }

    ListWindows() {
        return JSON.stringify(this._listWindows().map(window => this._describeWindow(window)));
    }

    _activateWindow(window) {
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
        const windows = this._listWindows();

        return windows.find(window => window.get_title() === title) ??
            windows.find(window => window.get_title()?.trim() === title) ??
            null;
    }

    _parseCriteria(criteriaJson) {
        const normalizedJson = `${criteriaJson}`.trim();
        if (!normalizedJson)
            return null;

        try {
            const parsed = JSON.parse(normalizedJson);
            return typeof parsed === 'object' && parsed !== null ? parsed : null;
        } catch (_) {
            return null;
        }
    }

    _listWindows() {
        return global.get_window_actors()
            .map(actor => actor.meta_window)
            .filter(window => window && !window.skip_taskbar)
            .sort((left, right) => this._windowUserTime(right) - this._windowUserTime(left));
    }

    _windowUserTime(window) {
        if (!window)
            return 0;

        if (typeof window.get_user_time === 'function') {
            try {
                return window.get_user_time();
            } catch (_) {
                return 0;
            }
        }

        return 0;
    }

    _describeWindow(window) {
        return {
            title: readWindowString(window, 'get_title'),
            wmClass: readWindowString(window, 'get_wm_class'),
            wmClassInstance: readWindowString(window, 'get_wm_class_instance'),
            gtkApplicationId: readWindowString(window, 'get_gtk_application_id', 'gtk_application_id'),
            sandboxedAppId: readWindowString(window, 'get_sandboxed_app_id', 'sandboxed_app_id'),
            focused: Boolean(window?.has_focus?.()),
            minimized: Boolean(window?.minimized),
        };
    }

    _findWindowMatching(criteria) {
        return this._listWindows().find(window => this._matchesWindow(window, criteria)) ?? null;
    }

    _matchesWindow(window, criteria) {
        const info = this._describeWindow(window);

        return (
            this._matchesExact(info.title, criteria.title) &&
            this._matchesContains(info.title, criteria.titleContains) &&
            this._matchesExact(info.wmClass, criteria.wmClass) &&
            this._matchesExact(info.wmClassInstance, criteria.wmClassInstance) &&
            this._matchesAppId(info, criteria.appId) &&
            this._matchesExact(info.gtkApplicationId, criteria.gtkApplicationId) &&
            this._matchesExact(info.sandboxedAppId, criteria.sandboxedAppId)
        );
    }

    _matchesAppId(info, value) {
        if (value === undefined || value === null || `${value}`.trim() === '')
            return true;

        const wanted = normalizeValue(value);
        return [
            info.gtkApplicationId,
            info.sandboxedAppId,
        ].some(candidate => normalizeValue(candidate) === wanted);
    }

    _matchesExact(actual, expected) {
        if (expected === undefined || expected === null || `${expected}`.trim() === '')
            return true;

        return normalizeValue(actual) === normalizeValue(expected);
    }

    _matchesContains(actual, expected) {
        if (expected === undefined || expected === null || `${expected}`.trim() === '')
            return true;

        return normalizeValue(actual).includes(normalizeValue(expected));
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
