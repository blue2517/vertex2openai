from typing import Optional
from google.genai import types
import config as app_config


def get_http_options(base_url: Optional[str] = None) -> Optional[types.HttpOptions]:
    """构造 google-genai HTTP 选项：代理、自定义证书，以及可选的 base_url 覆盖。

    base_url 的来源优先级：显式入参 > 环境变量 VERTEX_BASE_URL > 不设置（SDK 默认）。
    说明：Express（api_key）模式下 SDK 默认解析出的就是全局端点
    https://aiplatform.googleapis.com/（project/location 均为 None，URL 无 location 段），
    所以正常情况下**不需要**设这个变量。
    """
    client_args = {}
    if app_config.PROXY_URL:
        client_args["proxy"] = app_config.PROXY_URL
    if app_config.SSL_CERT_FILE:
        client_args["verify"] = app_config.SSL_CERT_FILE

    base_url = base_url or (app_config.VERTEX_BASE_URL or None)
    if base_url:
        return types.HttpOptions(
            base_url=base_url,
            client_args=client_args if client_args else None,
            async_client_args=client_args if client_args else None,
        )
    if client_args:
        return types.HttpOptions(
            client_args=client_args,
            async_client_args=client_args,
        )
    return None
