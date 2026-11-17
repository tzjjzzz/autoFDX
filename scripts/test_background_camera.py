"""Run on the game PC; prints whether UE consumed relative input packets."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autofdx.config import ConfigStore
from autofdx.window_service import WindowService


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dx',type=int,default=10)
    ap.add_argument('--steps',type=int,default=12)
    ap.add_argument('--delay',type=int,default=8)
    args=ap.parse_args()
    if not 1<=args.steps<=100 or not -1000<=args.dx<=1000 or not 1<=args.delay<=60:
        ap.error('steps: 1..100; dx: -1000..1000; delay: 1..60')
    config=ConfigStore();config.load()
    ws=WindowService(config)
    backend=ws.get_input()
    if backend is None:raise SystemExit('Windows required')
    hwnd=ws.get_game_window()._hWnd
    print('Enter a scene where the physical mouse normally rotates the camera; switch away during countdown.')
    for left in range(args.delay,0,-1):print(left,flush=True);time.sleep(1)
    report={'dx':args.dx,'steps':args.steps,'errors':[]}
    try:
        for _ in range(args.steps):
            if backend._api.foreground()==hwnd:raise RuntimeError('Game foreground; background test stopped')
            backend.camera_move_rel(args.dx,0)
            time.sleep(.05)
        time.sleep(1)
        report['raw_input']=backend.camera_status()
        print(json.dumps(report,indent=2))
        print('dataReads > 0 confirms packet consumption, not camera rotation. Observe actual view.')
    except Exception as exc:
        report['errors'].append(str(exc));print('FAILED:',exc)
    finally:
        report['cleanup_errors']=ws.shutdown_input()
        out=Path(__file__).resolve().parents[1]/'logs'
        out.mkdir(exist_ok=True)
        path=out/('camera_probe_'+time.strftime('%Y%m%d_%H%M%S')+'.json')
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(report,indent=2),encoding='utf-8');temp.replace(path)
        print('Log:',path)

if __name__=='__main__':main()
