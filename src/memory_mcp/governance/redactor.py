"""PII redaction. Raw user text passes through here before any LLM sees it."""

from dataclasses import dataclass
from typing import Any, Protocol

# Checked against the public infoType reference
# (docs.cloud.google.com/sensitive-data-protection/docs/infotypes-reference).
# Run scripts/check_dlp_info_types.py to confirm them for your project:
# DLP rejects the whole request if any name is unknown, which fails closed.
# Not used, on purpose: LOCATION (would remove the user's own city) and
# SECURITY_DATA (a broad general type; the specific credential types are listed).
INFO_TYPES = (
    # contact
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "STREET_ADDRESS",
    "IP_ADDRESS",
    # people other than the user's own role and city
    "PERSON_NAME",
    "DATE_OF_BIRTH",
    # payment and bank
    "CREDIT_CARD_NUMBER",
    "CREDIT_CARD_TRACK_NUMBER",
    "IBAN_CODE",
    "FINANCIAL_ACCOUNT_NUMBER",
    # government ids
    "INDIA_AADHAAR_INDIVIDUAL",
    "INDIA_PAN_INDIVIDUAL",
    "INDIA_PASSPORT",
    "INDIA_GST_INDIVIDUAL",
    "PASSPORT",
    "DRIVERS_LICENSE_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    # credentials and secrets
    "PASSWORD",
    "AUTH_TOKEN",
    "BASIC_AUTH_HEADER",
    "JSON_WEB_TOKEN",
    "OAUTH_CLIENT_SECRET",
    "ENCRYPTION_KEY",
    "AWS_CREDENTIALS",
    "AZURE_AUTH_TOKEN",
    "GCP_API_KEY",
    "GCP_CREDENTIALS",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HTTP_COOKIE",
    "XSRF_TOKEN",
)

DLP_TIMEOUT_SECONDS = 15


class RedactorError(RuntimeError):
    """Redaction could not be done. Callers must fail closed. Never carries content."""


@dataclass(frozen=True)
class Redaction:
    text: str  # findings replaced with [INFO_TYPE] tokens
    findings: int


class Redactor(Protocol):
    def redact(self, text: str) -> Redaction: ...


class DlpRedactor:
    """Cloud DLP (Sensitive Data Protection) de-identification by infoType replacement."""

    def __init__(self, project_id: str, location: str = "global", client: Any = None) -> None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import dlp_v2

        self._dlp = dlp_v2
        self._client = client or dlp_v2.DlpServiceClient(
            client_options=ClientOptions(quota_project_id=project_id)
        )
        self._parent = f"projects/{project_id}/locations/{location}"
        self._inspect_config = dlp_v2.InspectConfig(
            info_types=[dlp_v2.InfoType(name=name) for name in INFO_TYPES]
        )
        # One explicit replacement per infoType, so the token format is ours: [EMAIL_ADDRESS].
        self._deidentify_config = dlp_v2.DeidentifyConfig(
            info_type_transformations=dlp_v2.InfoTypeTransformations(
                transformations=[
                    dlp_v2.InfoTypeTransformations.InfoTypeTransformation(
                        info_types=[dlp_v2.InfoType(name=name)],
                        primitive_transformation=dlp_v2.PrimitiveTransformation(
                            replace_config=dlp_v2.ReplaceValueConfig(
                                new_value=dlp_v2.Value(string_value=f"[{name}]")
                            )
                        ),
                    )
                    for name in INFO_TYPES
                ]
            )
        )

    def redact(self, text: str) -> Redaction:
        from google.api_core import exceptions as gexc

        request = self._dlp.DeidentifyContentRequest(
            parent=self._parent,
            deidentify_config=self._deidentify_config,
            inspect_config=self._inspect_config,
            item=self._dlp.ContentItem(value=text),
        )
        try:
            response = self._client.deidentify_content(request=request, timeout=DLP_TIMEOUT_SECONDS)
        except (gexc.GoogleAPICallError, gexc.RetryError) as exc:
            raise RedactorError(f"DLP call failed: {type(exc).__name__}") from exc

        success = self._dlp.TransformationSummary.TransformationResultCode.SUCCESS
        findings = 0
        for summary in response.overview.transformation_summaries:
            for result in summary.results:
                if result.code != success:
                    # A finding that was not replaced could still be in the text.
                    raise RedactorError("DLP reported a transformation error")
                findings += result.count
        return Redaction(text=response.item.value, findings=findings)
