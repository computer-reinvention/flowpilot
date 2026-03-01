interface ContainerProps {
  children: React.ReactNode;
}

export function Container({ children }: ContainerProps) {
  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-6">{children}</div>
  );
}
