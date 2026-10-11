"""One-shot local order CLI; existing run.py owns MT5, AI3 and Feishu."""
import argparse
import os
from pathlib import Path
import sys
import time

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))
from crypto_demo.run import configure
from crypto_demo.instance_lock import account_id
from crypto_demo.manual_orders import ManualQueue


def main(argv=None):
    parser = argparse.ArgumentParser(description="向同目录运行中的模拟盘提交一笔订单；AI3护盘由现有run.py完成")
    parser.add_argument("--asset", required=True, choices=("BTC", "ETH"))
    parser.add_argument("--symbol", help="完整合约名；默认读取本目录.env")
    parser.add_argument("--side", choices=("buy", "sell"))
    parser.add_argument("--sl", type=float, help="固定止损价格")
    parser.add_argument("--tp", type=float, help="固定止盈价格")
    parser.add_argument("--volume", type=float, help="指定手数；不填则按现有现金风控计算，超限直接拒绝")
    parser.add_argument("--reason", default="按指定方向及SL/TP进场，形态依据未提供")
    parser.add_argument("--invalidation", default="原SL触发；其他结构失效条件未知")
    parser.add_argument("--target-basis", default="固定TP，目标结构依据未提供")
    args = parser.parse_args(argv)
    symbol = configure(args.asset, "demo", args.symbol)
    if not symbol:
        parser.error("本目录.env未填写CRYPTO_"+args.asset+"_SYMBOL")
    try:
        login, server = os.environ.get("MT5_LOGIN", ""), os.environ.get("MT5_SERVER", "")
        identity = account_id(login, server)
        magic = int(os.environ.get("FIB_PA_AI_MAGIC", "240611"))
        side = args.side or input("方向 buy/sell: ").strip().lower()
        sl = args.sl if args.sl is not None else float(input("止损价格: "))
        tp = args.tp if args.tp is not None else float(input("止盈价格: "))
        queue = ManualQueue(BASE/"crypto_logs"/identity/args.asset/"manual_orders",
                            identity, args.asset, symbol, magic)
        request = queue.submit(side, sl, tp, args.volume, args.reason, args.invalidation, args.target_basis)
        identifier = request["request_id"]
        print("已提交，等待主程序执行。请求编号: "+identifier, flush=True)
        deadline = time.monotonic()+75
        while time.monotonic() < deadline:
            result = queue.result(identifier)
            if result is not None:
                print(result["message"])
                if result.get("ticket"):
                    print("ticket: "+str(result["ticket"]))
                return 0 if result["status"] == "filled" else 2
            time.sleep(.5)
        print("尚未收到确认，可能正在等待上一轮AI或经纪商回执。请核对主程序/MT5；本请求不会自动重发。")
        return 2
    except KeyboardInterrupt:
        print("退出等待不会撤回已提交的请求；请核对主程序/MT5，避免重复提交。")
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("未确认开仓: "+str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
