"""A stand-in for a SPIRE agent's Workload API, for tests only.

It speaks the real protocol (the gRPC service generated from SPIFFE's
workload.proto, as shipped in the `spiffe` package) over a Unix socket, and
signs real JWT-SVIDs with an EC key, as SPIRE does. A real agent tells
workloads apart by checking which container is calling; here each identity
gets its own socket, and every socket shares one signing key, so a token
issued on one validates on another, as within one trust domain.

    with FakeAgent(tmp_path) as agent:
        backend_socket = agent.socket_for("spiffe://warden.local/backend")"""
import time
import uuid
from concurrent import futures
from pathlib import Path

import grpc
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf import struct_pb2
from spiffe._proto import workload_pb2, workload_pb2_grpc

HEADER = ("workload.spiffe.io", "true")


class _Service(workload_pb2_grpc.SpiffeWorkloadAPIServicer):
    def __init__(self, agent: "FakeAgent", spiffe_id: str):
        self.agent, self.spiffe_id = agent, spiffe_id

    def _check_header(self, context):
        if HEADER not in context.invocation_metadata():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "security header missing from request")

    def FetchJWTSVID(self, request, context):
        self._check_header(context)
        if not request.audience:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "audience must be specified")
        token = self.agent.issue(self.spiffe_id, list(request.audience))
        return workload_pb2.JWTSVIDResponse(svids=[workload_pb2.JWTSVID(spiffe_id=self.spiffe_id, svid=token)])

    def ValidateJWTSVID(self, request, context):
        self._check_header(context)
        try:
            claims = jwt.decode(request.svid, self.agent.key.public_key(), algorithms=["ES256"],
                                audience=request.audience, options={"require": ["exp", "sub", "aud"]})
        except jwt.PyJWTError as e:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"unable to validate JWT: {e}")
        struct = struct_pb2.Struct()
        struct.update({k: v for k, v in claims.items() if k != "aud"})
        return workload_pb2.ValidateJWTSVIDResponse(spiffe_id=claims["sub"], claims=struct)


class FakeAgent:
    def __init__(self, folder: Path, ttl: int = 300):
        self.folder, self.ttl = Path(folder), ttl
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.kid = uuid.uuid4().hex
        self.issued: list[str] = []
        self._servers = []

    def issue(self, spiffe_id: str, audience: list[str], ttl: int | None = None) -> str:
        now = int(time.time())
        token = jwt.encode({"sub": spiffe_id, "aud": audience, "iat": now, "exp": now + (ttl or self.ttl)},
                           self.key, algorithm="ES256", headers={"kid": self.kid, "typ": "JWT"})
        self.issued.append(token)
        return token

    def socket_for(self, spiffe_id: str) -> str:
        path = self.folder / f"agent-{len(self._servers)}.sock"
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        workload_pb2_grpc.add_SpiffeWorkloadAPIServicer_to_server(_Service(self, spiffe_id), server)
        server.add_insecure_port(f"unix:{path}")
        server.start()
        self._servers.append(server)
        return f"unix://{path}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for server in self._servers:
            server.stop(grace=None)
