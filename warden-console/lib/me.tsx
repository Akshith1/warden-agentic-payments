"use client";
import { createContext, useContext } from "react";

export type Me = { signedIn: boolean; username?: string; name?: string; roles?: string[] };
export const MeContext = createContext<Me>({ signedIn: false });
export const useMe = () => useContext(MeContext);
export const can = (me: Me, ...roles: string[]) => roles.some((r) => me.roles?.includes(r));
