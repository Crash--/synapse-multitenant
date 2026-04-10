import type { FastifyRequest, FastifyReply } from "fastify";

export function createAuthHook(apiToken: string) {
  return async function authenticate(
    request: FastifyRequest,
    reply: FastifyReply
  ) {
    const auth = request.headers.authorization;
    if (!auth || !auth.startsWith("Bearer ")) {
      return reply
        .status(401)
        .send({ error: "Missing or invalid Authorization header" });
    }

    const token = auth.slice("Bearer ".length);
    if (token !== apiToken) {
      return reply.status(403).send({ error: "Invalid API token" });
    }
  };
}
