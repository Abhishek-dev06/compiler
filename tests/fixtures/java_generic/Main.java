import java.util.Scanner;

public class Main {
    @Deprecated
    static final class ArrayStack<T> {
        private final T value;

        ArrayStack(T value) {
            this.value = value;
        }

        T peek() {
            return value;
        }
    }

    public static void main(String[] args) {
        Scanner scanner = new Scanner(System.in);
        ArrayStack<String> stack = new ArrayStack<>(scanner.nextLine());
        System.out.println("Java works: " + stack.peek());
    }
}
