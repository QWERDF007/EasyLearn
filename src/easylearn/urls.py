from typing import Annotated

from pydantic import AfterValidator, AnyHttpUrl


def _validate_service_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if any(
        part is not None for part in (value.username, value.password, value.query, value.fragment)
    ):
        raise ValueError("Service URL must not include credentials, query or fragment")
    return value


ServiceUrl = Annotated[AnyHttpUrl, AfterValidator(_validate_service_url)]
