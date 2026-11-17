// Execute the real agent with mocked Frida memory/APIs: no game or OS input.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('autofdx/background_input_agent.js', 'utf8');
for (const pointerSize of [4, 8]) {
    const regions = [];
    let next = 0x100000n;
    class Pointer {
        constructor(n) { this.n = n instanceof Pointer ? n.n : BigInt(n); }
        add(n) { return new Pointer(this.n + BigInt(n)); }
        compare(p) { return this.n < p.n ? -1 : this.n > p.n ? 1 : 0; }
        equals(p) { return this.n === p.n; }
        isNull() { return this.n === 0n; }
        toString() { return '0x' + this.n.toString(16); }
        region() {
            const row = regions.find(r => this.n >= r.base && this.n < r.base + BigInt(r.buffer.length));
            assert.ok(row, 'memory must be allocated');
            return [row.buffer, Number(this.n - row.base)];
        }
        writeU32(x) { const [b,o]=this.region();b.writeUInt32LE(x>>>0,o); }
        readU32() { const [b,o]=this.region();return b.readUInt32LE(o); }
        writeS32(x) { const [b,o]=this.region();b.writeInt32LE(x,o); }
        readS32() { const [b,o]=this.region();return b.readInt32LE(o); }
        writeU16(x) { const [b,o]=this.region();b.writeUInt16LE(x,o); }
        readU16() { const [b,o]=this.region();return b.readUInt16LE(o); }
        writePointer(p) { const [b,o]=this.region();pointerSize===8?b.writeBigUInt64LE(p.n,o):b.writeUInt32LE(Number(p.n),o); }
        readPointer() { const [b,o]=this.region();return new Pointer(pointerSize===8?b.readBigUInt64LE(o):b.readUInt32LE(o)); }
        writeByteArray(a) { const [b,o]=this.region();Buffer.from(a).copy(b,o); }
        readByteArray(n) { const [b,o]=this.region();return Uint8Array.from(b.subarray(o,o+n)).buffer; }
    }
    const ptr = n => new Pointer(n);
    const memory = {alloc(n) { const p=ptr(next);regions.push({base:next,buffer:Buffer.alloc(n)});next+=BigInt(n+32);return p; }};
    const replacements = {};
    const context = {
        ptr, Memory:memory, Process:{pointerSize,mainModule:{base:ptr(0x400000),size:0x10000},
            getCurrentThreadId:()=>1,getModuleByName:()=>({getExportByName:n=>n})},
        Interceptor:{attach:()=>{},replace:(name,cb)=>replacements[name]=cb},
        NativeCallback:function(fn){return fn;},
        NativeFunction:function(){return ()=>0xeeee;},
        rpc:{exports:{}}, send:()=>{}, Date,
    };
    vm.runInNewContext(source,context);
    const rpc=context.rpc.exports;
    rpc.configure('0x100');rpc.prepare(100,100);
    const header=pointerSize===8?24:16;
    const total=header+24;
    const token=rpc.queueRaw(5000,-20);
    const size=memory.alloc(4);size.writeU32(0);
    const read=replacements.GetRawInputData;
    const callCtx={};
    assert.equal(read.call(callCtx,ptr(token),0x10000003,ptr(0),size,header),0);
    assert.equal(size.readU32(),total);
    const output=memory.alloc(total);
    size.writeU32(total-1);
    assert.equal(read.call(callCtx,ptr(token),0x10000003,output,size,header),0xffffffff);
    assert.equal(callCtx.lastError,122);
    size.writeU32(total);
    assert.equal(read.call(callCtx,ptr(token),0x10000003,output,size,header),total);
    assert.equal(output.readU32(),0);
    assert.equal(output.add(4).readU32(),total);
    assert.equal(output.add(8+pointerSize).readPointer().toString(),'0x1');
    assert.equal(output.add(header).readU16(),0);
    assert.equal(output.add(header+12).readS32(),5000);
    assert.equal(output.add(header+16).readS32(),-20);
    assert.equal(rpc.rawStatus().dataReads,1);
    assert.equal(rpc.rawStatus().pending,0);
    assert.equal(read.call(callCtx,ptr(999),0x10000003,ptr(0),size,header),0xeeee);
    size.writeU32(header);
    assert.equal(read.call(callCtx,ptr(token),0x10000005,output,size,header),header);
    assert.equal(read.call(callCtx,ptr(token),0x10000003,output,size,header-1),0xffffffff);
    const cancelled=rpc.queueRaw(1,2);rpc.cancelRaw(cancelled);
    assert.equal(rpc.rawStatus().cancelled,1);
    rpc.stop();assert.equal(rpc.rawStatus().pending,0);
    console.log(`PASS ${pointerSize*8}-bit header/layout, relative values, size query, errors, pass-through, cancellation`);
}
