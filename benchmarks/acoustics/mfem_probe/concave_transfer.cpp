#include "mfem.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{

constexpr double kPi = 3.141592653589793238462643383279502884;

struct SampleResult
{
   double frequency_hz = 0.0;
   double pressure_real_pa = 0.0;
   double pressure_imag_pa = 0.0;
   int iterations = 0;
   double relative_residual = 0.0;
};

struct OrderResult
{
   int order = 0;
   int ndofs = 0;
   int elements = 0;
   double assemble_s = 0.0;
   double solve_s = 0.0;
   int max_iterations = 0;
   double max_relative_residual = 0.0;
   std::vector<SampleResult> samples;
};

mfem::Mesh BuildFrozenConcaveLRoom()
{
   // Exact R100A-2 wave-concave-l-room-v1 prism. The horizontal polygon is
   // (0,0)-(6,0)-(6,4)-(4,4)-(4,2)-(2,2)-(2,4)-(0,4), extruded to z=2.5.
   // Five conforming 2 m x 2 m x 2.5 m hexahedra tile the volume without any
   // rectangular fill of the concave 2 m x 2 m notch.
   mfem::Mesh mesh(3, 24, 5, 0, 3);

   const double xs[] = {0.0, 2.0, 4.0, 6.0};
   const double ys[] = {0.0, 2.0, 4.0};
   const double zs[] = {0.0, 2.5};

   for (double z : zs)
   {
      for (double y : ys)
      {
         for (double x : xs)
         {
            mesh.AddVertex(x, y, z);
         }
      }
   }

   auto vid = [](int ix, int iy, int iz)
   {
      return iz * 12 + iy * 4 + ix;
   };

   auto add_cell = [&](int ix, int iy)
   {
      mesh.AddHex(
         vid(ix,     iy,     0),
         vid(ix + 1, iy,     0),
         vid(ix + 1, iy + 1, 0),
         vid(ix,     iy + 1, 0),
         vid(ix,     iy,     1),
         vid(ix + 1, iy,     1),
         vid(ix + 1, iy + 1, 1),
         vid(ix,     iy + 1, 1),
         1);
   };

   add_cell(0, 0);
   add_cell(1, 0);
   add_cell(2, 0);
   add_cell(0, 1);
   add_cell(2, 1);

   mesh.FinalizeHexMesh(1, 0, true);
   return mesh;
}

OrderResult SolveOrder(
   int order,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   using clock = std::chrono::steady_clock;

   mfem::Mesh mesh = BuildFrozenConcaveLRoom();
   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);

   const auto assemble_started = clock::now();

   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator);
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::DeltaCoefficient source_delta(
      source_x, source_y, source_z, 1.0);
   mfem::LinearForm source_functional(&fes);
   source_functional.AddDomainIntegrator(
      new mfem::DomainLFIntegrator(source_delta));
   source_functional.Assemble();

   mfem::DeltaCoefficient receiver_delta(
      receiver_x, receiver_y, receiver_z, 1.0);
   mfem::LinearForm receiver_functional(&fes);
   receiver_functional.AddDomainIntegrator(
      new mfem::DomainLFIntegrator(receiver_delta));
   receiver_functional.Assemble();

   const auto assemble_finished = clock::now();

   mfem::SparseMatrix &K = stiffness.SpMat();
   mfem::SparseMatrix &M = mass.SpMat();

   mfem::Vector solution(fes.GetTrueVSize());
   solution = 0.0;
   bool have_initial_guess = false;

   OrderResult result;
   result.order = order;
   result.ndofs = fes.GetTrueVSize();
   result.elements = mesh.GetNE();
   result.assemble_s =
      std::chrono::duration<double>(assemble_finished - assemble_started).count();

   const auto solve_started = clock::now();

   const int frequency_count = static_cast<int>(
      std::llround((frequency_stop_hz - frequency_start_hz) / frequency_step_hz)) + 1;

   for (int index = 0; index < frequency_count; ++index)
   {
      const double frequency_hz = frequency_start_hz + index * frequency_step_hz;
      const double omega = 2.0 * kPi * frequency_hz;
      const double k = omega / sound_speed_m_s;

      // exp(-i*omega*t) authority:
      //   div(grad p) + k^2 p = i*omega*rho*Q*delta
      // becomes, after integration by parts with rigid Neumann walls,
      //   (grad p,grad v) - k^2(p,v) = -i*omega*rho*Q v(xs).
      // The operator is real; solve its imaginary component directly.
      std::unique_ptr<mfem::SparseMatrix> A(mfem::Add(1.0, K, -k * k, M));
      std::unique_ptr<mfem::SparseMatrix> P(mfem::Add(1.0, K,  k * k, M));

      mfem::Vector rhs(source_functional);
      rhs *= -omega * density_kg_m3 * source_amplitude_m3_s;

      // K-k^2 M is real symmetric but indefinite above the first cavity mode.
      // MINRES is the matching Krylov method.  The positive K+k^2 M diagonal
      // Jacobi preconditioner avoids introducing a non-symmetric solve path.
      mfem::DSmoother preconditioner(*P);
      mfem::MINRESSolver minres;
      minres.iterative_mode = have_initial_guess;
      minres.SetOperator(*A);
      minres.SetPreconditioner(preconditioner);
      minres.SetMaxIter(8000);
      minres.SetRelTol(1e-10);
      minres.SetAbsTol(0.0);
      minres.SetPrintLevel(0);

      if (!have_initial_guess)
      {
         solution = 0.0;
      }

      minres.Mult(rhs, solution);
      if (!minres.GetConverged())
      {
         throw std::runtime_error(
            "MINRES did not converge at order=" + std::to_string(order)
            + " frequency_hz=" + std::to_string(frequency_hz)
            + " iterations=" + std::to_string(minres.GetNumIterations())
            + " final_norm=" + std::to_string(minres.GetFinalNorm()));
      }
      have_initial_guess = true;

      mfem::Vector residual(rhs.Size());
      A->Mult(solution, residual);
      residual -= rhs;
      const double rhs_norm = rhs.Norml2();
      const double relative_residual =
         residual.Norml2() / std::max(rhs_norm, 1e-30);

      const double pressure_imag_pa = receiver_functional * solution;

      SampleResult sample;
      sample.frequency_hz = frequency_hz;
      sample.pressure_real_pa = 0.0;
      sample.pressure_imag_pa = pressure_imag_pa;
      sample.iterations = minres.GetNumIterations();
      sample.relative_residual = relative_residual;
      result.samples.push_back(sample);
      result.max_iterations = std::max(result.max_iterations, sample.iterations);
      result.max_relative_residual =
         std::max(result.max_relative_residual, sample.relative_residual);
   }

   const auto solve_finished = clock::now();
   result.solve_s =
      std::chrono::duration<double>(solve_finished - solve_started).count();
   return result;
}

void WriteJson(
   const std::string &path,
   const std::vector<OrderResult> &orders,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   double source_amplitude_m3_s,
   double frequency_start_hz,
   double frequency_stop_hz,
   double frequency_step_hz)
{
   std::ofstream os(path, std::ios::binary);
   if (!os) { throw std::runtime_error("cannot open output file: " + path); }

   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"r100b-mfem-concave-transfer-raw-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"fixture_geometry\": \"exact-r100a2-wave-concave-l-room-v1-five-hex-prism\",\n";
   os << "  \"boundary_model\": \"natural-neumann-rigid\",\n";
   os << "  \"fourier_sign\": \"exp(-i*omega*t)\",\n";
   os << "  \"linear_system\": \"K-k^2M\",\n";
   os << "  \"preconditioner\": \"diagonal-jacobi-on-K+k^2M\",\n";
   os << "  \"source_rhs\": \"-i*omega*rho*Q*delta\",\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": ["
      << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": ["
      << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_amplitude_m3_s\": " << source_amplitude_m3_s << ",\n";
   os << "  \"frequency_grid_hz\": {"
      << "\"start\":" << frequency_start_hz << ","
      << "\"stop\":" << frequency_stop_hz << ","
      << "\"step\":" << frequency_step_hz << "},\n";
   os << "  \"orders\": [\n";

   for (size_t oi = 0; oi < orders.size(); ++oi)
   {
      const auto &level = orders[oi];
      os << "    {\n";
      os << "      \"order\": " << level.order << ",\n";
      os << "      \"elements\": " << level.elements << ",\n";
      os << "      \"ndofs\": " << level.ndofs << ",\n";
      os << "      \"assemble_s\": " << level.assemble_s << ",\n";
      os << "      \"solve_s\": " << level.solve_s << ",\n";
      os << "      \"max_iterations\": " << level.max_iterations << ",\n";
      os << "      \"max_relative_residual\": " << level.max_relative_residual << ",\n";
      os << "      \"samples\": [\n";

      for (size_t si = 0; si < level.samples.size(); ++si)
      {
         const auto &sample = level.samples[si];
         os << "        {"
            << "\"frequency_hz\":" << sample.frequency_hz << ","
            << "\"pressure_real_pa\":" << sample.pressure_real_pa << ","
            << "\"pressure_imag_pa\":" << sample.pressure_imag_pa << ","
            << "\"iterations\":" << sample.iterations << ","
            << "\"relative_residual\":" << sample.relative_residual
            << "}" << (si + 1 == level.samples.size() ? "" : ",") << "\n";
      }

      os << "      ]\n";
      os << "    }" << (oi + 1 == orders.size() ? "" : ",") << "\n";
   }

   os << "  ]\n";
   os << "}\n";
}

} // namespace

int ProbeMain(int argc, char *argv[])
{
   double density_kg_m3 = 1.2;
   double sound_speed_m_s = 343.0;
   double source_x = 1.0, source_y = 1.0, source_z = 1.0;
   double receiver_x = 5.0, receiver_y = 1.0, receiver_z = 1.0;
   double source_amplitude_m3_s = 1.0;
   double frequency_start_hz = 20.0;
   double frequency_stop_hz = 300.0;
   double frequency_step_hz = 1.0;
   int order_min = 2;
   int order_max = 5;
   std::string output;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto value = [&](const char *name)
      {
         if (i + 1 >= argc)
         {
            throw std::runtime_error(std::string("missing value for ") + name);
         }
         return std::string(argv[++i]);
      };

      if (arg == "--density") { density_kg_m3 = std::stod(value("--density")); }
      else if (arg == "--sound-speed") { sound_speed_m_s = std::stod(value("--sound-speed")); }
      else if (arg == "--source-x") { source_x = std::stod(value("--source-x")); }
      else if (arg == "--source-y") { source_y = std::stod(value("--source-y")); }
      else if (arg == "--source-z") { source_z = std::stod(value("--source-z")); }
      else if (arg == "--receiver-x") { receiver_x = std::stod(value("--receiver-x")); }
      else if (arg == "--receiver-y") { receiver_y = std::stod(value("--receiver-y")); }
      else if (arg == "--receiver-z") { receiver_z = std::stod(value("--receiver-z")); }
      else if (arg == "--source-amplitude") { source_amplitude_m3_s = std::stod(value("--source-amplitude")); }
      else if (arg == "--frequency-start") { frequency_start_hz = std::stod(value("--frequency-start")); }
      else if (arg == "--frequency-stop") { frequency_stop_hz = std::stod(value("--frequency-stop")); }
      else if (arg == "--frequency-step") { frequency_step_hz = std::stod(value("--frequency-step")); }
      else if (arg == "--order-min") { order_min = std::stoi(value("--order-min")); }
      else if (arg == "--order-max") { order_max = std::stoi(value("--order-max")); }
      else if (arg == "--output") { output = value("--output"); }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (output.empty()) { throw std::runtime_error("--output is required"); }
   if (!(density_kg_m3 > 0.0 && sound_speed_m_s > 0.0 && source_amplitude_m3_s > 0.0))
   {
      throw std::runtime_error("density, sound speed and source amplitude must be positive");
   }
   if (!(frequency_start_hz > 0.0 && frequency_stop_hz >= frequency_start_hz
         && frequency_step_hz > 0.0))
   {
      throw std::runtime_error("invalid frequency grid");
   }
   if (order_min < 1 || order_max < order_min || order_max > 6)
   {
      throw std::runtime_error("invalid polynomial order range");
   }

   std::vector<OrderResult> results;
   for (int order = order_min; order <= order_max; ++order)
   {
      results.push_back(SolveOrder(
         order,
         density_kg_m3,
         sound_speed_m_s,
         source_x,
         source_y,
         source_z,
         receiver_x,
         receiver_y,
         receiver_z,
         source_amplitude_m3_s,
         frequency_start_hz,
         frequency_stop_hz,
         frequency_step_hz));
   }

   WriteJson(
      output,
      results,
      density_kg_m3,
      sound_speed_m_s,
      source_x,
      source_y,
      source_z,
      receiver_x,
      receiver_y,
      receiver_z,
      source_amplitude_m3_s,
      frequency_start_hz,
      frequency_stop_hz,
      frequency_step_hz);

   return 0;
}

int main(int argc, char *argv[])
{
   try
   {
      return ProbeMain(argc, argv);
   }
   catch (const std::exception &exc)
   {
      std::cerr << "R100B_MFEM_CONCAVE_FATAL: " << exc.what() << std::endl;
      return 2;
   }
   catch (...)
   {
      std::cerr << "R100B_MFEM_CONCAVE_FATAL: unknown exception" << std::endl;
      return 3;
   }
}
