import { currentPerson } from "@/lib/session";

export async function GET() {
  const person = await currentPerson();
  return Response.json(person ? { signedIn: true, ...person } : { signedIn: false });
}
