"""
Intern InkStone (discovery.intern-ai.org.cn) 非官方 Python SDK
=============================================================
逆向自前端 RemoteApp.js / 对话模块网络抓包。覆盖：
  - 认证 (login / get_user_info / logout)
  - 对话 (models / new chat / send message SSE / title / chat list)
  - 科学计算·开发机管理 (list / create / start / stop / delete / rename / resource / log / monitor / renewal)
  - 科学计算·云盘管理 (list / mkdir / rename / move / copy / delete / upload / download / quota)
  - 科学计算·训练任务 / 推理服务 / 镜像 (list/create/stop/delete 等)

认证方式：请求头 Authorization: Bearer <uaa-token>，uaa-token 为 JWT，来源 cookie uaa-token。
"""

import json
import time
import uuid
from typing import Any, Dict, Generator, Iterator, List, Optional

import requests

DEFAULT_BASE = "https://discovery.intern-ai.org.cn"


class InkStoneError(Exception):
    """SDK 通用异常。"""


class APIError(InkStoneError):
    def __init__(self, status: int, code: Any, msg: str, raw: Any = None):
        self.status = status
        self.code = code
        self.msg = msg
        self.raw = raw
        super().__init__(f"[{status}] code={code} msg={msg}")


class InkStoneClient:
    """Intern InkStone 平台客户端。

    用法：
        client = InkStoneClient(token="uaa-token 的值")
        # 或从浏览器 cookie 自动读取
        client = InkStoneClient.from_cookie_jar(...)

        models = client.list_models()
        cid = client.new_chat()
        for ev in client.send_message(chat_id=cid, query="你好"):
            print(ev)
    """

    def __init__(self, token: Optional[str] = None, base: str = DEFAULT_BASE,
                 session: Optional[requests.Session] = None, timeout: int = 30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.token = token
        if token:
            self.session.headers.update(self._auth_headers(token))
        self.session.headers.setdefault("Content-Type", "application/json")
        self.session.headers.setdefault("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

    # ---------- 认证 ----------
    @staticmethod
    def _auth_headers(token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def set_token(self, token: str) -> None:
        self.token = token
        self.session.headers.update(self._auth_headers(token))

    @classmethod
    def from_cookie(cls, cookie_str: str, **kw) -> "InkStoneClient":
        """从 'uaa-token=xxx; ...' 形式的 cookie 字符串构造。"""
        token = None
        for part in cookie_str.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "uaa-token":
                token = v
                break
        if not token:
            raise InkStoneError("cookie 中未找到 uaa-token")
        c = cls(token=token, **kw)
        c.session.headers.setdefault("Cookie", cookie_str)
        return c

    # ---------- 通用请求 ----------
    def _request(self, method: str, path: str, *, params: Any = None,
                 json_body: Any = None, data: Any = None, headers: Any = None,
                 stream: bool = False) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        resp = self.session.request(method, url, params=params, json=json_body,
                                    data=data, headers=headers, stream=stream,
                                    timeout=self.timeout)
        if stream:
            return resp
        return self._check(resp)

    @staticmethod
    def _check(resp: requests.Response) -> requests.Response:
        if resp.status_code >= 400:
            try:
                j = resp.json()
            except Exception:
                j = None
            if isinstance(j, dict):
                raise APIError(resp.status_code, j.get("code"), j.get("msg", ""), j)
            raise APIError(resp.status_code, None, resp.text[:200], resp.text)
        return resp

    def _json(self, resp: requests.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            raise InkStoneError(f"非 JSON 响应: {resp.text[:200]}")

    @staticmethod
    def _data(j: Any) -> Any:
        """统一取 data 字段。"""
        if isinstance(j, dict) and "data" in j:
            return j["data"]
        return j

    # ============================================================
    # 认证模块
    # ============================================================
    def login(self, code: str) -> Any:
        """用 SSO 回调 code 换取 jwt。返回包含 token 的对象。

        前端逻辑：Rt.post('/api/v1/users/auth', {data:{code}, forbidCheckLogin:true})
        """
        r = self._request("POST", "/api/v1/users/auth", json_body={"code": code})
        return self._json(r)

    def get_user_info(self, jwt: str) -> Any:
        """用 jwt 获取用户信息。"""
        r = self._request("POST", "/api/v1/users/getUserInfo", json_body={"jwt": jwt})
        return self._json(r)

    def logout(self, jwt: str) -> Any:
        r = self._request("POST", "/api/v1/users/logout", json_body={"jwt": jwt})
        return self._json(r)

    # ============================================================
    # 对话模块 (orbit)
    # ============================================================
    def list_models(self) -> List[Dict[str, Any]]:
        """获取可用模型列表。返回 data.models=[{name,tags,icon}]。"""
        r = self._request("GET", "/api/orbit/v1/models")
        j = self._json(r)
        data = self._data(j)
        return data.get("models", data) if isinstance(data, dict) else data

    def list_chats(self, page: int = 1, size: int = 20, scenario: int = 1) -> Any:
        """会话列表。"""
        r = self._request("GET", "/api/orbit/v1/chat/list",
                          params={"page": page, "size": size, "scenario": scenario})
        return self._data(self._json(r))

    def new_chat(self, scenario: int = 1, space_id: int = 0, paper_id: int = 0) -> int:
        """新建会话，返回 chat_id。"""
        r = self._request("POST", "/api/orbit/v1/chat/new",
                          json_body={"scenario": scenario, "space_id": space_id, "paper_id": paper_id})
        data = self._data(self._json(r))
        if isinstance(data, dict):
            return data.get("id")
        return data

    def generate_title(self, chat_id: int) -> Any:
        """为会话生成标题。"""
        r = self._request("POST", f"/api/orbit/v1/chat/{chat_id}/title")
        return self._json(r)

    def send_message(self, chat_id: int, query: str, *, model: str = "GLM-5.2",
                     thinking_mode: bool = True, planning_mode: bool = False,
                     scenario: int = 1) -> Iterator[Dict[str, Any]]:
        """发送消息，流式返回 SSE 事件。

        事件类型(event 字段)：
          workflow_started -> message(thinking/answer 增量) -> message_end
            -> followup(推荐问题) -> workflow_finished

        每个 event 是一个 dict，含 event/cid/mid 及业务字段。
        """
        body = {
            "scenario": scenario,
            "thinking_mode": thinking_mode,
            "planning_mode": planning_mode,
            "model": model,
            "query": query,
            "chat_id": chat_id,
        }
        resp = self.session.post(f"{self.base}/api/orbit/v1/chat", json=body,
                                 stream=True, timeout=self.timeout,
                                 headers={"Accept": "text/event-stream"})
        self._check(resp)
        yield from self._iter_sse(resp)

    def send_message_text(self, chat_id: int, query: str, **kw) -> str:
        """send_message 的便捷封装：拼出完整回答文本。"""
        answer = []
        for ev in self.send_message(chat_id, query, **kw):
            if ev.get("event") == "message" and ev.get("type") == "answer":
                answer.append(ev.get("content", ""))
        return "".join(answer)

    @staticmethod
    def _iter_sse(resp: requests.Response) -> Iterator[Dict[str, Any]]:
        """解析 text/event-stream：以 data: 行为单位的 JSON 事件。"""
        event = {"event": "message"}
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.rstrip("\r\n")
            if line == "":
                # 事件分隔
                if event:
                    yield event
                event = {"event": "message"}
                continue
            if line.startswith(":"):
                continue
            if ":" in line:
                field, _, value = line.partition(":")
                value = value.lstrip(" ")
                if field == "data":
                    try:
                        event.update(json.loads(value))
                    except Exception:
                        event.setdefault("raw", []).append(value)
                else:
                    event[field] = value
        if event and len(event) > 1:
            yield event

    # ============================================================
    # 科学计算·开发机管理 (dev-machine)
    # ============================================================
    def dev_machine_list(self, page: int = 1, size: int = 100, keyword: str = "",
                         sort_type: str = "time_desc") -> Any:
        """开发机列表。"""
        r = self._request("GET", "/api/ml/v1/dev-machine/list",
                          params={"page": page, "size": size, "keyword": keyword,
                                  "sort_type": sort_type})
        return self._data(self._json(r))

    def dev_machine_user_resource(self) -> Any:
        """用户资源配额：算力点 / notebook 数量。"""
        r = self._request("GET", "/api/ml/v1/dev-machine/user_resource")
        return self._data(self._json(r))

    def dev_machine_create(self, machine: Dict[str, Any]) -> Any:
        """创建开发机。machine 为完整创建对象，常见字段：
        name, mirror_id, resource_id(算力规格), duration, dataset(可空), desc 等。
        建议先 list_mirrors / computility_resources 取可选值。"""
        r = self._request("POST", "/api/ml/v1/dev-machine/create", json_body=machine)
        return self._data(self._json(r))

    def dev_machine_start(self, dev_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/dev-machine/start", json_body={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_stop(self, dev_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/dev-machine/stop", json_body={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_delete(self, dev_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/dev-machine/delete", json_body={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_rename(self, dev_id: int, new_name: str) -> Any:
        r = self._request("POST", "/api/ml/v1/dev-machine/rename",
                          json_body={"id": dev_id, "new_name": new_name})
        return self._data(self._json(r))

    def dev_machine_enter(self, dev_id: int) -> Any:
        """进入开发机，返回 VSCode/JupyterLab 入口信息。"""
        r = self._request("POST", "/api/ml/v1/dev-machine/enter", json_body={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_dev_log(self, dev_id: int) -> Any:
        r = self._request("GET", "/api/ml/v1/dev-machine/dev-log",
                          params={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_monitor(self, dev_id: int) -> Any:
        r = self._request("GET", "/api/ml/v1/dev-machine/monitor",
                          params={"id": dev_id})
        return self._data(self._json(r))

    def dev_machine_renewal(self, dev_id: int, duration: int) -> Any:
        r = self._request("POST", "/api/ml/v1/dev-machine/renewal",
                          json_body={"id": dev_id, "duration": duration})
        return self._data(self._json(r))

    def dev_machine_remaining_time(self, dev_id: int) -> Any:
        r = self._request("GET", "/api/ml/v1/dev-machine/remaining-time",
                          params={"id": dev_id})
        return self._data(self._json(r))

    def computility_resources(self, category: str = "notebook") -> Any:
        """可选算力规格。"""
        r = self._request("GET", "/api/ml/v1/dev-machine/computility_resources",
                          params={"category": category})
        return self._data(self._json(r))

    # ============================================================
    # 科学计算·云盘管理 (cloud-disk)
    # ============================================================
    def cloud_disk_list(self, dir_path: str = "/", page_size: int = 100,
                       page_token: str = "", keyword: str = "", sort_by: str = "",
                       sort_order: str = "", dirs_only: bool = False,
                       recursive: bool = False) -> Any:
        """云盘文件列表。返回 data.list/total/page/size。"""
        params = {"dir_path": dir_path, "page_size": page_size,
                  "keyword": keyword, "dirs_only": dirs_only, "recursive": recursive}
        if page_token:
            params["page_token"] = page_token
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order
        r = self._request("GET", "/api/ml/v1/cloud-disk/list", params=params)
        return self._data(self._json(r))

    def cloud_disk_quota(self) -> Any:
        """云盘配额。"""
        r = self._request("GET", "/api/ml/v1/cloud-disk/quota")
        return self._data(self._json(r))

    def cloud_disk_mkdir(self, dir_path: str, dir_name: str) -> Any:
        r = self._request("POST", "/api/ml/v1/cloud-disk/mkdir",
                          json_body={"dir_path": dir_path, "dir_name": dir_name})
        return self._data(self._json(r))

    def cloud_disk_rename(self, src_key: str, new_name: str,
                          conflict_strategy: str = "rename") -> Any:
        r = self._request("POST", "/api/ml/v1/cloud-disk/rename",
                          json_body={"src_key": src_key, "new_name": new_name,
                                     "conflict_strategy": conflict_strategy})
        return self._data(self._json(r))

    def cloud_disk_move(self, src_keys: List[str], dst_dir: str,
                        conflict_strategy: str = "rename") -> Any:
        r = self._request("POST", "/api/ml/v1/cloud-disk/move",
                          json_body={"src_keys": src_keys, "dst_dir": dst_dir,
                                     "conflict_strategy": conflict_strategy})
        return self._data(self._json(r))

    def cloud_disk_copy(self, src_keys: List[str], dst_dir: str,
                        conflict_strategy: str = "rename") -> Any:
        r = self._request("POST", "/api/ml/v1/cloud-disk/copy",
                          json_body={"src_keys": src_keys, "dst_dir": dst_dir,
                                     "conflict_strategy": conflict_strategy})
        return self._data(self._json(r))

    def cloud_disk_delete(self, file_keys: List[str]) -> Any:
        r = self._request("POST", "/api/ml/v1/cloud-disk/delete",
                          json_body={"file_keys": file_keys})
        return self._data(self._json(r))

    def cloud_disk_upload_url(self, dir_path: str, files: List[Dict[str, Any]]) -> Any:
        """获取上传 URL。files=[{name,size,...}]，返回预签名 URL 等。"""
        r = self._request("POST", "/api/ml/v1/cloud-disk/upload-url",
                          json_body={"dir_path": dir_path, "files": files})
        return self._data(self._json(r))

    def cloud_disk_upload_file(self, dir_path: str, local_path: str) -> Any:
        """便捷上传：先取上传 URL，再 PUT 文件。"""
        import os
        size = os.path.getsize(local_path)
        name = os.path.basename(local_path)
        info = self.cloud_disk_upload_url(dir_path, [{"name": name, "size": size}])
        # info 结构通常含预签名 URL；按实际返回上传
        upload_url = None
        if isinstance(info, dict):
            upload_url = info.get("upload_url") or info.get("url")
            items = info.get("list") or info.get("files") or []
            if not upload_url and items:
                upload_url = items[0].get("upload_url") or items[0].get("url")
        if not upload_url:
            return info
        with open(local_path, "rb") as f:
            put = requests.put(upload_url, data=f,
                               headers={"Content-Type": "application/octet-stream"})
        return {"info": info, "status": put.status_code}

    def cloud_disk_download_url(self, file_keys: List[str]) -> Any:
        """获取下载 URL。"""
        r = self._request("POST", "/api/ml/v1/cloud-disk/download-url",
                          json_body={"file_keys": file_keys})
        return self._data(self._json(r))

    def cloud_disk_check(self, dst_dir: str, file_names: List[str]) -> Any:
        """检查目标目录是否已存在同名文件。"""
        r = self._request("POST", "/api/ml/v1/cloud-disk/check",
                          json_body={"dst_dir": dst_dir, "file_names": file_names})
        return self._data(self._json(r))

    def cloud_disk_default_name_list(self, dir_path: str, is_dir: bool = False) -> Any:
        r = self._request("GET", "/api/ml/v1/cloud-disk/default-name-list",
                          params={"dir_path": dir_path, "is_dir": is_dir})
        return self._data(self._json(r))

    # ============================================================
    # 科学计算·镜像管理 (mirrors)
    # ============================================================
    def list_mirrors(self, page: int = 1, size: int = 20, keyword: str = "",
                     building_status: str = "", category: str = "",
                     sort_type: str = "update_time_desc") -> Any:
        params = {"page": page, "size": size, "keyword": keyword,
                  "sort_type": sort_type}
        if building_status:
            params["building_status"] = building_status
        if category:
            params["category"] = category
        r = self._request("GET", "/api/ml/v1/mirrors", params=params)
        return self._data(self._json(r))

    def list_system_mirrors(self, **kw) -> Any:
        """便捷：系统镜像（开发机可选镜像）。"""
        kw.setdefault("category", "notebook")
        kw.setdefault("building_status", "success")
        return self.list_mirrors(**kw)

    def mirrors_limit(self) -> Any:
        r = self._request("GET", "/api/ml/v1/mirrors/limit")
        return self._data(self._json(r))

    # ============================================================
    # 科学计算·训练任务 (training)
    # ============================================================
    def training_list(self, page: int = 1, size: int = 20, status: str = "") -> Any:
        params = {"page": page, "size": size}
        if status:
            params["status"] = status
        r = self._request("GET", "/api/ml/v1/training/list", params=params)
        return self._data(self._json(r))

    def training_list_monitor(self, ids: List[int]) -> Any:
        """监控多个训练任务状态。"""
        r = self._request("GET", "/api/ml/v1/training/list-monitor",
                          params=[("ids", i) for i in ids])
        return self._data(self._json(r))

    def training_create(self, task: Dict[str, Any]) -> Any:
        r = self._request("POST", "/api/ml/v1/training/create", json_body=task)
        return self._data(self._json(r))

    def training_stop(self, task_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/training/stop", json_body={"id": task_id})
        return self._data(self._json(r))

    def training_delete(self, task_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/training/delete", json_body={"id": task_id})
        return self._data(self._json(r))

    def training_update_name(self, task_id: int, name: str) -> Any:
        r = self._request("POST", "/api/ml/v1/training/update_name",
                          json_body={"id": task_id, "name": name})
        return self._data(self._json(r))

    def training_extend_duration(self, task_id: int, duration: int) -> Any:
        r = self._request("POST", "/api/ml/v1/training/extend_duration",
                          json_body={"id": task_id, "duration": duration})
        return self._data(self._json(r))

    def training_code_upload_url(self, file_name: str) -> Any:
        r = self._request("GET", "/api/ml/v1/training/code_upload_url",
                          params={"file_name": file_name})
        return self._data(self._json(r))

    # ============================================================
    # 科学计算·推理服务 (inference)
    # ============================================================
    def inference_list(self, page: int = 1, size: int = 20, status: str = "") -> Any:
        params = {"page": page, "size": size}
        if status:
            params["status"] = status
        r = self._request("GET", "/api/ml/v1/inference/list", params=params)
        return self._data(self._json(r))

    def inference_create(self, service: Dict[str, Any]) -> Any:
        r = self._request("POST", "/api/ml/v1/inference/create", json_body=service)
        return self._data(self._json(r))

    def inference_start(self, service_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/inference/start", json_body={"id": service_id})
        return self._data(self._json(r))

    def inference_stop(self, service_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/inference/stop", json_body={"id": service_id})
        return self._data(self._json(r))

    def inference_delete(self, service_id: int) -> Any:
        r = self._request("POST", "/api/ml/v1/inference/delete", json_body={"id": service_id})
        return self._data(self._json(r))

    def inference_rename(self, service_id: int, name: str) -> Any:
        r = self._request("POST", "/api/ml/v1/inference/rename",
                          json_body={"id": service_id, "name": name})
        return self._data(self._json(r))

    # ============================================================
    # 资源配置 (resource)
    # ============================================================
    def computility_cfg(self) -> Any:
        """算力规格配置。"""
        r = self._request("GET", "/api/ml/v1/resource/computility-cfg")
        return self._data(self._json(r))

    def capacity_cfg(self) -> Any:
        """云盘容量申请配置。"""
        r = self._request("GET", "/api/ml/v1/resource/capacity-cfg")
        return self._data(self._json(r))
