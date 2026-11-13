// The household shell (ADR-0024): one application over N core instances.
// Discovery: ask the instance that served this page for its household;
// every member gets a client; unreachable members are shown, not hidden.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { InstanceClient, LauncherClient, selfMember, type InstanceInfo, type Member, type Principal } from "./api/client";

export interface MemberState {
  member: Member;
  client: InstanceClient;
  info: InstanceInfo | null;
  error: string | null;
}

interface Household {
  members: MemberState[];
  /** The launcher's URL when this household is launcher-managed (ADR-0024); "" = same origin (ADR-0032). */
  launcher: string | null;
  selected: string | "all";
  select: (id: string | "all") => void;
  /** Members currently in scope (all reachable, or the selected one). */
  scope: MemberState[];
  reload: () => Promise<void>;
  loading: boolean;
  /** Login state (ADR-0025): required and nobody signed in → the shell shows the login. */
  authRequired: boolean;
  principal: Principal | null;
  /** Hosted instance: the platform page that signs in, signs out and switches areas. */
  accountUrl: string | null;
  login: (name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  launcherClient: LauncherClient | null;
}

const HouseholdContext = createContext<Household | null>(null);

export function HouseholdProvider({ children }: { children: ReactNode }) {
  const [members, setMembers] = useState<MemberState[]>([]);
  const [launcher, setLauncher] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | "all">("all");
  const [loading, setLoading] = useState(true);
  const [authRequired, setAuthRequired] = useState(false);
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [accountUrl, setAccountUrl] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    const self = new InstanceClient(selfMember());
    let list: Member[];
    let launcherUrl: string | null = null;
    let required = false;
    // A hosted instance has no launcher: the platform signs people in.
    let hosted: Principal | null = null;
    let loginUrl: string | null = null;
    try {
      const hh = await self.household();
      list = hh.members.length ? hh.members : [selfMember()];
      // The member whose id the serving instance reports is "us": address it
      // relatively so development (Vite proxy) and production (/app) both work.
      list = list.map((m) => (m.id === hh.self ? { ...m, url: window.location.origin } : m));
      launcherUrl = hh.launcher ?? null;
      required = Boolean(hh.auth_required);
      hosted = hh.principal ?? null;
      loginUrl = hh.login_url ?? null;
    } catch {
      list = [selfMember()];
    }
    setLauncher(launcherUrl);
    setAuthRequired(required);
    if (required) {
      // Ask the launcher who we are before touching the instances; without
      // a session there is nothing to load and no point in N times 401.
      let who: Principal | null = null;
      // "" = the launcher at this origin (ADR-0032); null = no launcher at all.
      if (launcherUrl !== null) {
        try {
          who = (await new LauncherClient(launcherUrl).me()).principal;
        } catch {
          who = null;
        }
      }
      if (launcherUrl === null) who = hosted;
      setPrincipal(who);
      setAccountUrl(loginUrl);
      if (!who && launcherUrl === null && loginUrl) {
        // Nobody signed in and no login page of our own: the platform has it.
        window.location.assign(loginUrl);
        return;
      }
      if (!who) {
        setMembers(list.map((member) => ({ member, client: new InstanceClient(member), info: null, error: null })));
        setLoading(false);
        return;
      }
    } else {
      setPrincipal(null);
    }
    const states = await Promise.all(
      list.map(async (member): Promise<MemberState> => {
        const client = new InstanceClient(member);
        try {
          const info = await client.instance();
          return { member: { ...member, title: member.title || info.title }, client, info, error: null };
        } catch (err) {
          return { member, client, info: null, error: err instanceof Error ? err.message : String(err) };
        }
      }),
    );
    setMembers(states);
    setLoading(false);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // An instance answered 401: the session expired or was revoked.
  useEffect(() => {
    const onUnauthorized = () => setPrincipal((p) => (p ? null : p));
    window.addEventListener("aitmation:unauthorized", onUnauthorized);
    return () => window.removeEventListener("aitmation:unauthorized", onUnauthorized);
  }, []);

  const login = useCallback(
    async (name: string, password: string) => {
      if (launcher === null) throw new Error("no launcher");
      await new LauncherClient(launcher).login(name, password);
      await reload();
    },
    [launcher, reload],
  );
  const logout = useCallback(async () => {
    if (launcher === null && accountUrl) {
      // Signed in at the platform: it also signs out.
      window.location.assign(accountUrl);
      return;
    }
    if (launcher !== null) await new LauncherClient(launcher).logout().catch(() => undefined);
    setPrincipal(null);
    setMembers((ms) => ms.map((m) => ({ ...m, info: null })));
  }, [launcher, accountUrl]);

  const value = useMemo<Household>(() => {
    const reachable = members.filter((m) => m.info !== null);
    const scope = selected === "all" ? reachable : reachable.filter((m) => m.member.id === selected);
    return {
      members,
      launcher,
      selected,
      select: setSelected,
      scope,
      reload,
      loading,
      authRequired,
      principal,
      accountUrl,
      login,
      logout,
      launcherClient: launcher !== null ? new LauncherClient(launcher) : null,
    };
  }, [members, launcher, selected, reload, loading, authRequired, principal, accountUrl, login, logout]);

  return <HouseholdContext.Provider value={value}>{children}</HouseholdContext.Provider>;
}

export function useHousehold(): Household {
  const ctx = useContext(HouseholdContext);
  if (!ctx) throw new Error("useHousehold outside HouseholdProvider");
  return ctx;
}

/** Fetch something from every member in scope and tag rows with the member. */
export function useAcrossHousehold<T>(
  fetcher: (client: InstanceClient) => Promise<T[]>,
  deps: unknown[] = [],
): { rows: (T & { _member: Member })[]; loading: boolean; error: string | null; refresh: () => void } {
  const { scope } = useHousehold();
  const [rows, setRows] = useState<(T & { _member: Member })[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const scopeKey = scope.map((m) => m.member.id).join(",");
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all(
      scope.map(async (m) => {
        const list = await fetcher(m.client);
        return list.map((row) => ({ ...row, _member: m.member }));
      }),
    )
      .then((lists) => {
        if (!cancelled) {
          setRows(lists.flat());
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, tick, ...deps]);
  return { rows, loading, error, refresh: () => setTick((n) => n + 1) };
}
