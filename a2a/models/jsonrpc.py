"""JSON-RPC 2.0 request and response models.

Implements the JSON-RPC 2.0 specification for CSTP method calls.
See: https://www.jsonrpc.org/specification
"""

from dataclasses import dataclass, field
from typing import Any


# JSON-RPC 2.0 Error Codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Custom CSTP Error Codes
AUTHENTICATION_REQUIRED = -32001
RATE_LIMITED = -32002
QUERY_FAILED = -32003
GUARDRAIL_EVAL_FAILED = -32004
PERMISSION_DENIED = -32009


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    """JSON-RPC 2.0 error object.

    Attributes:
        code: Error code (negative integer).
        message: Short error description.
        data: Additional error data (optional).
    """

    code: int
    message: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass(slots=True)
class JsonRpcRequest:
    """JSON-RPC 2.0 request object.

    Attributes:
        method: Method name to invoke.
        params: Method parameters.
        id: Request identifier for correlation.
        jsonrpc: Protocol version (must be "2.0").
    """

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str | int | None = None
    jsonrpc: str = "2.0"

    def validate(self) -> JsonRpcError | None:
        """Validate request format.

        Returns:
            JsonRpcError if invalid, None if valid.
        """
        if self.jsonrpc != "2.0":
            return JsonRpcError(
                code=INVALID_REQUEST,
                message="Invalid JSON-RPC version",
                data={"expected": "2.0", "got": self.jsonrpc},
            )
        if not self.method:
            return JsonRpcError(
                code=INVALID_REQUEST,
                message="Method is required",
            )
        if not self.method.startswith("cstp."):
            return JsonRpcError(
                code=METHOD_NOT_FOUND,
                message=f"Unknown method: {self.method}",
                data={"method": self.method},
            )
        if not isinstance(self.params, dict):
            return JsonRpcError(
                code=INVALID_PARAMS,
                message="Params must be an object (named parameters only)",
                data={"got": type(self.params).__name__},
            )
        return None


@dataclass(slots=True)
class JsonRpcResponse:
    """JSON-RPC 2.0 response object.

    Attributes:
        id: Request identifier (matches request).
        result: Method result (mutually exclusive with error).
        error: Error object (mutually exclusive with result).
        jsonrpc: Protocol version (always "2.0").
    """

    id: str | int | None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        response: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
        }
        if self.error is not None:
            response["error"] = self.error.to_dict()
        else:
            response["result"] = self.result
        return response

    @classmethod
    def success(cls, id: str | int | None, result: dict[str, Any]) -> "JsonRpcResponse":
        """Create a success response."""
        return cls(id=id, result=result)

    @classmethod
    def failure(cls, id: str | int | None, error: JsonRpcError) -> "JsonRpcResponse":
        """Create an error response."""
        return cls(id=id, error=error)
