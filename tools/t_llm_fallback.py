# -*- coding: utf-8 -*-
"""LLM 兜底路径测试：不起真实 LLM，用假 HTTP 服务验证
   ① 未配置时完全不发请求
   ② 配置后候选词确实进了 TMDB 检索
   ③ LLM 返回垃圾时不写库
"""
import sys, io, os, json, threading, time, http.server
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, sync, tmdb, llm

db.init_db()

# ---- ① 未配置 -> 必须不调用
cfg = config.load()
print("① 未配置时 llm.enabled() =", llm.enabled())
assert llm.enabled() is False

# ---- 假 LLM 服务
HITS = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        HITS.append(body)
        if MOCK_MODE[0] == "good":
            content = '{"candidates": ["Live and Let Die", "007：你死我活"], "year": 1973}'
        elif MOCK_MODE[0] == "fenced":
            content = '```json\n{"candidates": ["Licence to Kill"], "year": 1989}\n```'
        else:  # garbage
            content = '抱歉，我不知道这是什么电影。'
        out = {"choices": [{"message": {"content": content}}]}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

MOCK_MODE = ["good"]
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---- ② 配置 LLM，走一遍 _llm_retry
config.save({"llm": {
    "enabled": True,
    "base_url": "http://127.0.0.1:%d/v1" % port,
    "api_key": "sk-test-not-real",
    "model": "gpt-test",
    "timeout": 10,
}})
print("② 配置后 llm.enabled() =", llm.enabled())
assert llm.enabled() is True

item = db.get_item(115)          # 之生死关头
assert item, "item 115 不存在"
payload = sync._payload_from_item(item)
print("   payload.raw =", payload.get("raw"))
print("   payload.queries =", payload.get("queries"))

client = tmdb.Tmdb(config.load().get("tmdb") or {})
out = sync._llm_retry(item, payload, client, "movie", config.load())
print("   _llm_retry ->", out and (out.get("title"), out.get("year"),
                                   out.get("tmdb_id"), out.get("source"),
                                   out.get("llm_candidates")))
print("   fake LLM 收到 %d 次请求" % len(HITS))
if HITS:
    msg = HITS[-1]["messages"][-1]["content"]
    print("   prompt 片段:", msg[:120].replace("\n", " "))

# ---- ③ 垃圾输出
MOCK_MODE[0] = "garbage"
out2 = sync._llm_retry(item, payload, client, "movie", config.load())
print("③ LLM 返回非 JSON -> _llm_retry =", out2)
assert out2 is None, "垃圾输出必须被拒绝"

# ---- ④ 围栏 + 候选词正确
MOCK_MODE[0] = "fenced"
out3 = sync._llm_retry(item, payload, client, "movie", config.load())
print("④ 带代码块围栏 -> _llm_retry =", out3 and (out3.get("title"),
      out3.get("year"), out3.get("tmdb_id")))

srv.shutdown()
# 清掉测试配置，避免污染真实运行
config.save({"llm": {"enabled": False, "api_key": "", "model": "",
                     "base_url": ""}})
print()
print("测试配置已还原：llm.enabled() =", llm.enabled())

# item 115 应仍保持未识别（_llm_retry 不写库）
after = db.get_item(115)
print("item 115 状态:", after.get("status"), "| tmdb_id:", after.get("tmdb_id"))
assert after.get("tmdb_id") is None, "_llm_retry 不应该直接改库"

print()
print("全部断言通过 ✓")
