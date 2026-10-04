import { RunScreen } from "@/components/run-screen";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunScreen id={id} />;
}
