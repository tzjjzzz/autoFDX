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

// Synthetic HRAWINPUT tokens are owned by this agent, never physical HID input.
// Only our tokens are handled here; genuine OS raw input passes through unchanged.
const rawPackets = new Map();
const rawStats = {queued:0, sizeQueries:0, dataReads:0, expired:0, cancelled:0};
const rawHeaderSize = Process.pointerSize === 8 ? 24 : 16;
const rawPacketSize = rawHeaderSize + 24;
const rawAddress = u.getExportByName('GetRawInputData');
const originalRaw = new NativeFunction(rawAddress, 'uint', ['pointer','uint','pointer','pointer','uint']);
function pruneRaw() {
    const now = Date.now();
    for (const [key, row] of rawPackets) {
        if (now-row.created>5000) {
            if (!row.read) rawStats.expired++;
            rawPackets.delete(key);
        }
    }
}
const rawCallback = new NativeCallback(function(handle, command, data, size, headerSize) {
    const row = rawPackets.get(handle.toString());
    if (!row) return originalRaw(handle, command, data, size, headerSize);
    if (size.isNull() || headerSize !== rawHeaderSize) {
        this.lastError = 87;
        return 0xffffffff;
    }
    const needed = command === 0x10000003 ? rawPacketSize : command === 0x10000005 ? rawHeaderSize : 0;
    if (!needed) { this.lastError=87;return 0xffffffff; }
    if (data.isNull()) {
        size.writeU32(needed);rawStats.sizeQueries++;return 0;
    }
    if (size.readU32()<needed) {
        size.writeU32(needed);this.lastError=122;return 0xffffffff;
    }
    data.writeByteArray(row.buffer.readByteArray(needed));
    size.writeU32(needed);
    if(command===0x10000003) {
        if(!row.read)rawStats.dataReads++;
        row.read=true;
    }
    return needed;
}, 'uint', ['pointer','uint','pointer','pointer','uint']);
callbacks.push(rawCallback);
Interceptor.replace(rawAddress, rawCallback);

rpc.exports = {
    configure(hwnd) { target = ptr(hwnd); enabled = false; needsActivation = true; },
    prepare(x, y) {
        xy = {x:x, y:y}; enabled = true;
        return needsActivation;
    },
    queueRaw(dx, dy) {
        if(!enabled)throw new Error('input bridge is not armed');
        if(!Number.isInteger(dx)||!Number.isInteger(dy)||Math.abs(dx)>2147483647||Math.abs(dy)>2147483647)
            throw new Error('invalid relative delta');
        pruneRaw();
        // Remove consumed packets first, retaining unread packets until timeout.
        if(rawPackets.size>=128)for(const [key,row] of rawPackets)if(row.read)rawPackets.delete(key);
        if(rawPackets.size>=128)throw new Error('Raw Input queue not being consumed; camera delivery stopped');
        const buffer=Memory.alloc(rawPacketSize);
        buffer.writeByteArray(new Uint8Array(rawPacketSize).buffer);
        buffer.writeU32(0); // RIM_TYPEMOUSE
        buffer.add(4).writeU32(rawPacketSize);
        // hDevice = NULL; wParam = RIM_INPUTSINK (background).
        buffer.add(8+Process.pointerSize).writePointer(ptr(1));
        const mouse=buffer.add(rawHeaderSize);
        mouse.writeU16(0); // MOUSE_MOVE_RELATIVE
        mouse.add(12).writeS32(dx);
        mouse.add(16).writeS32(dy);
        const token=buffer.toString();
        rawPackets.set(token,{buffer:buffer,created:Date.now(),read:false});
        rawStats.queued++;
        return token;
    },
    cancelRaw(token) { if(rawPackets.delete(token))rawStats.cancelled++; },
    rawStatus() { pruneRaw();return Object.assign({pending:Array.from(rawPackets.values()).filter(r=>!r.read).length},rawStats); },
    stop() { enabled = false; captures.clear();rawPackets.clear(); }
};
