from qbwc_kit.wsdl import build_qwc

from .config import Settings


def render_qwc(s: Settings) -> str:
    return build_qwc(
        app_name=s.qwc_app_name,
        app_id="",
        app_url=s.app_url,
        app_description=s.qwc_app_description,
        username=s.qbwc_username,
        owner_id="{%s}" % str(s.qwc_owner_id).upper(),
        file_id="{%s}" % str(s.qwc_file_id).upper(),
        run_every_n_seconds=s.run_every_n_seconds,
        support_url=s.qwc_support_url or s.app_url,
    )
