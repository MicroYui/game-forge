import { Link, useLocation } from "react-router-dom";

export type ReauthenticationLocationState = {
  forceReauthentication: true;
  returnTo: string;
};

export function ReauthenticationLink({ className = "gf-secondary-button" }: { className?: string }) {
  const location = useLocation();
  const state: ReauthenticationLocationState = {
    forceReauthentication: true,
    returnTo: `${location.pathname}${location.search}`,
  };
  return (
    <Link className={className} state={state} to="/login">
      重新登录
    </Link>
  );
}
