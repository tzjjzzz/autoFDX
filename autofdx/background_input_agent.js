// Derived from the user-verified V6 protocol. No fixed game offsets.
let enabled = false;
let needsActivation = true;
let target = ptr(0);
let xy = {x: 0, y: 0};
const captures = new Map();
const main = Process.mainModule;
const end = main.base.add(main.size);
const own = p => p.compare(main.base) >= 0 && p.compare(end) < 0;
const u = Process.getModuleByName('user32.dll');
const callbacks = [];

Interceptor.attach(u.getExportByName('GetCursorPos'), {
    onEnter(args) { this.change = enabled && own(this.returnAddress); this.out = args[0]; },
    onLeave(ret) {
        if (this.change && !this.out.isNull()) {
            try { this.out.writeS32(xy.x); this.out.add(4).writeS32(xy.y); ret.replace(1); }
            catch(e) { send({kind: 'error', detail: String(e)}); }
        }
    }
});
for (const name of ['SetCursorPos', 'ClipCursor']) {
    const types = name === 'SetCursorPos' ? ['int', 'int'] : ['pointer'];
    const address = u.getExportByName(name);
    const original = new NativeFunction(address, 'int', types);
    const callback = new NativeCallback(function(...args) {
        if (enabled && own(this.returnAddress)) return 1;
        return original(...args);
    }, 'int', types);
    callbacks.push(callback);
    Interceptor.replace(address, callback);
}
const setAddress = u.getExportByName('SetCapture');
const originalSet = new NativeFunction(setAddress, 'pointer', ['pointer']);
const setCallback = new NativeCallback(function(hwnd) {
    if (enabled && own(this.returnAddress)) {
        const tid = Process.getCurrentThreadId();
        const previous = captures.get(tid) || ptr(0);
        captures.set(tid, hwnd);
        return previous;
    }
    return originalSet(hwnd);
}, 'pointer', ['pointer']);
callbacks.push(setCallback);
Interceptor.replace(setAddress, setCallback);
Interceptor.attach(u.getExportByName('GetCapture'), {
    onEnter() { this.change = enabled && own(this.returnAddress); this.tid = Process.getCurrentThreadId(); },
    onLeave(ret) { if (this.change && captures.has(this.tid)) ret.replace(captures.get(this.tid)); }
});
const releaseAddress = u.getExportByName('ReleaseCapture');
const originalRelease = new NativeFunction(releaseAddress, 'int', []);
const releaseCallback = new NativeCallback(function() {
    const tid = Process.getCurrentThreadId();
    if (enabled && own(this.returnAddress) && captures.has(tid)) {
        captures.set(tid, ptr(0));
        return 1;
    }
    return originalRelease();
}, 'int', []);
callbacks.push(releaseCallback);
Interceptor.replace(releaseAddress, releaseCallback);
for (const name of ['DispatchMessageW', 'DispatchMessageA']) {
    Interceptor.attach(u.getExportByName(name), {
        onEnter(args) {
            if (!enabled) return;
            try {
                const msg = args[0];
                if (msg.readPointer().equals(target) && msg.add(Process.pointerSize).readU32() === 0x001c) {
                    const off = Process.pointerSize === 8 ? 16 : 8;
                    needsActivation = msg.add(off).readPointer().isNull();
                }
            } catch(e) { send({kind:'error', detail:String(e)}); }
        }
    });
}
rpc.exports = {
    configure(hwnd) { target = ptr(hwnd); enabled = false; needsActivation = true; },
    prepare(x, y) {
        xy = {x:x, y:y}; enabled = true;
        return needsActivation;
    },
    stop() { enabled = false; captures.clear(); }
};
