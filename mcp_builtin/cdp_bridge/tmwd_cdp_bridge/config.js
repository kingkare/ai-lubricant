const TID = '__cdp_bridge_mcp_request_9b8d7f6a';
// Bridge 连接地址（单一输入框，可填 ws:// 或 wss:// 完整地址）。
// MCP Runtime 已合并进主程序，桥接 WebSocket 挂在主服务端口（默认 8001）上，
// 路径 /mcp/cdp-bridge/session。以接入教程里显示的地址为准，此处仅为占位默认。
const DEFAULT_BRIDGE_URL = 'ws://127.0.0.1:8001/mcp/cdp-bridge/session';
// 客户端 token 必须由 Ai Lubricant 管理端生成。
const DEFAULT_CLIENT_TOKEN = '';

// 随机密码生成器（content / popup / background 共用，避免三份实现漂移）。
// length 截断到 8~128，默认 16；四类字符（大小写+数字+符号）各保底一个，再
// 从并集补齐，Fisher-Yates 用 crypto.getRandomValues 打散。
function tmwdGeneratePassword(length) {
  const size = Math.max(8, Math.min(128, Number(length) || 16));
  const lower = 'abcdefghijklmnopqrstuvwxyz';
  const upper = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const digits = '0123456789';
  const symbols = '!@#$%^&*()-_=+[]{};:,.?';
  const all = lower + upper + digits + symbols;
  const rnd = () => crypto.getRandomValues(new Uint32Array(1))[0];
  const pick = (chars) => chars[rnd() % chars.length];
  const chars = [pick(lower), pick(upper), pick(digits), pick(symbols)];
  while (chars.length < size) chars.push(pick(all));
  for (let i = chars.length - 1; i > 0; i--) {
    const j = rnd() % (i + 1);
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join('');
}

