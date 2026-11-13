// Landing page of a navigation group (ADR-0029): one card per member with
// a short description, so a folded group is still discoverable.
import { Card, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";
import { useI18n } from "../i18n";
import { type NavGroup } from "../nav";

export function GroupOverview({ group }: { group: NavGroup }) {
  const { t } = useI18n();
  return (
    <Stack>
      <Title order={2}>{t(`nav.${group.key}`)}</Title>
      <Text c="dimmed" size="sm">{t(`overview.${group.key}`)}</Text>
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
        {group.children
          .filter((c) => c.to !== group.base)
          .map((c) => (
            <Card key={c.to} withBorder component={Link} to={c.to} style={{ textDecoration: "none" }}>
              <Title order={5}>{t(`nav.${c.key}`)}</Title>
              <Text size="sm" c="dimmed" mt={4}>{t(`overview.${c.key}`)}</Text>
            </Card>
          ))}
      </SimpleGrid>
    </Stack>
  );
}
