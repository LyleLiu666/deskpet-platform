import { execFile } from "node:child_process";
import { basename } from "node:path";

const command = /* DESKPET_COMMAND */ [];

export const DeskPetNotifications = async ({ directory }) => {
  const finished = new Map();
  let delivery = Promise.resolve();

  function send(event) {
    delivery = delivery.then(() => new Promise((resolve) => {
      const child = execFile(command[0], command.slice(1), { timeout: 2000 }, () => resolve());
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(event));
    })).catch(() => {});
    return delivery;
  }

  return {
    event({ event: { type, properties = {} } }) {
      if (type === "message.updated") {
        const message = properties.info;
        if (message?.role === "user") finished.delete(message.sessionID);
        if (message?.role === "assistant") {
          if (message.finish === "stop" && message.time?.completed && !message.error && !message.summary) {
            finished.set(message.sessionID, message.id);
          } else {
            finished.delete(message.sessionID);
          }
        }
        return;
      }

      const session = properties.sessionID;
      if (type === "session.status" && properties.status?.type === "idle") {
        const id = finished.get(session);
        finished.delete(session);
        if (id) return send({ kind: "completed", session, id, title: basename(directory ?? "") });
      } else if (type === "session.error") {
        finished.delete(session);
      } else if (type === "session.deleted") {
        finished.delete(properties.info?.id);
      } else if (type === "question.asked" || type === "permission.asked") {
        const request = type === "question.asked" ? "question" : "permission";
        const title = request === "question" ? properties.questions?.[0]?.header : properties.permission;
        return send({ kind: "asked", session, request, id: properties.id, title });
      } else if (["question.replied", "question.rejected", "permission.replied"].includes(type)) {
        const request = type === "permission.replied" ? "permission" : "question";
        return send({ kind: "resolved", session, request, id: properties.requestID });
      }
    },
  };
};
